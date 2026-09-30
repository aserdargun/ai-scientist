"""S2 diagnostic with explicit top_k=20; this is a review request override.

The fixed value follows the pinned Qwen coding profile and avoids the measured
vLLM split top-p forced-token failure. Product source is unchanged; this probe
does not count as unmodified production acceptance.
"""
from __future__ import annotations

import review_local_qwen_s2_token_boundaries as boundaries

base = boundaries.base
native = boundaries.native
ORIGINAL_PROFILE = base.profile_dict


def read_with_topk(socket_path, method, path, payload, **kwargs):
    if method == "POST" and path == "/v1/chat/completions":
        payload = {**payload, "top_k": 20}
    return boundaries.read_with_ids(socket_path, method, path, payload, **kwargs)


def diagnostic_profile():
    return {**ORIGINAL_PROFILE(), "review_request_top_k_override": 20,
            "production_profile_modified": False}


if __name__ == "__main__":
    boundaries.configure()
    base.__file__ = __file__
    base.PROBE_SCHEMA = "local-qwen-s2-topk20-diagnostic.v1"
    base.SOURCE_PATHS += (
        "docs/ai-scientist/review-evidence/review_local_qwen_s2_topk_diagnostic.py",
    )
    base.profile_dict = diagnostic_profile
    native._read_uds_json = read_with_topk
    raise SystemExit(base.main_cli())
