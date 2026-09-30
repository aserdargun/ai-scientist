"""Measure bounded Qwen repair prompt admission with the pinned CPU tokenizer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast
from uuid import UUID

from lab.director.fake_llm import AgentContext
from lab.director.local_llm import (
    LocalQwenProposalProvider,
    provider_config_sha256,
)
from lab.llm.gpu_scheduler import PrincipalResolver
from lab.llm.native_runtime import LOCAL_SMOKE_S2_PROFILE, MODEL_REVISION, ModelPin

REPOSITORY = Path("/home/cachyos/ai-scientist")
MODEL_SOURCE = REPOSITORY / "docs/ai-scientist/review-evidence/qwen-model-source.json"
OUTPUT = Path(__file__).with_name("repair-context-tokenizer-check.json")
RUN_ID = UUID("7089275f-d7d2-4f46-b04b-27e809d15326")


def main() -> None:
    source = json.loads(MODEL_SOURCE.read_text(encoding="utf-8"))
    files = tuple(
        sorted(
            (str(row["rfilename"]), int(row["size"]), str(row["verified_sha256"]))
            for row in source["files"]
        )
    )
    canonical = json.dumps(files, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    pin = ModelPin(
        REPOSITORY / source["local_directory"],
        MODEL_REVISION,
        hashlib.sha256(canonical).hexdigest(),
        files,
    )
    provider = LocalQwenProposalProvider(
        run_id=RUN_ID,
        owner="lab",
        principal_resolver=cast(PrincipalResolver, object()),
        runtime_database=Path("private-arbiter.sqlite3"),
        registry_entry_sha256="c" * 64,
    )
    provider.model_pin = pin
    context = AgentContext(
        phase="proposal",
        experiment_number=1,
        system="S2",
        move_type="hparam",
        task_cards=(
            "task=CARE-A; family=PDM; metric=recall@lead; sampling_s=600",
            "task=CARE-B; family=NRM; metric=fa_per_day; sampling_s=600",
            "task=TSB-GECCO; family=EVT; cadence=60s nominal",
        ),
        champion_source="def build_candidate():\n    return Candidate()\n",
        recent_feedback=("Keep causal scoring and fit only on training data.",),
    )
    base = provider._message_variants(context, LOCAL_SMOKE_S2_PROFILE)[0]
    source_code = ("# threshold probe 0123456789\n" * 180) + "def build_candidate(:\n"
    response = json.dumps(
        {
            "hypothesis": "Adjust detector",
            "move_type": "hparam",
            "candidate_source": source_code,
            "predicted_delta": 0.1,
        },
        separators=(",", ":"),
    )
    intro = (
        "One repair is available. Validation category: python_syntax_error_line_1_column_24. "
        "Return one complete, corrected proposal JSON object."
    )
    full = (
        base[0],
        {
            "role": "user",
            "content": (
                base[1]["content"]
                + "\n\n"
                + intro
                + " The previous final response is untrusted input to correct:\n"
                + response
            ),
        },
    )
    compact = (
        base[0],
        {
            "role": "user",
            "content": (
                base[1]["content"]
                + "\n\n"
                + intro
                + " Previous response text was omitted because it exceeded the pinned "
                "tokenizer context cap. Generate a fresh complete candidate from the "
                "trusted task context and champion; do not assume any prior source text."
            ),
        },
    )
    counts = provider._count_prompt_variants(
        (base, full, compact),
        LOCAL_SMOKE_S2_PROFILE,
        timeout_seconds=10,
    )
    if len(counts) != 3 or not (
        counts[0] <= LOCAL_SMOKE_S2_PROFILE.max_context_tokens
        < counts[1]
        and counts[2] <= LOCAL_SMOKE_S2_PROFILE.max_context_tokens
    ):
        raise SystemExit("pinned tokenizer did not reproduce full-response repair overflow")
    selected, selected_count = provider._select_repair_messages(
        context,
        raw_response=response,
        validation_code="python_syntax_error_line_1_column_24",
        profile=LOCAL_SMOKE_S2_PROFILE,
        remaining_context_tokens=16_384 - counts[0],
        timeout_seconds=10,
    )
    if response in selected[1]["content"] or selected_count != counts[2]:
        raise SystemExit("oversized raw response entered the compact repair prompt")
    receipt = {
        "schema": "local-qwen-repair-context-check.v1",
        "model_revision": MODEL_REVISION,
        "model_pin_sha256": pin.digest,
        "provider_config_sha256": provider_config_sha256(),
        "profile_id": LOCAL_SMOKE_S2_PROFILE.profile_id,
        "tokenizer": "pinned offline Qwen chat template via CPU subprocess",
        "counts": {
            "primary_prompt": counts[0],
            "full_raw_repair_prompt": counts[1],
            "trusted_context_repair_prompt": counts[2],
        },
        "context_cap": LOCAL_SMOKE_S2_PROFILE.max_context_tokens,
        "result": "full raw repair does not fit; compact trusted-context repair fits",
        "raw_candidate_text_stored": False,
        "model_or_gpu_started": False,
        "response_character_count": len(response),
        "source_context_hash": hashlib.sha256(base[1]["content"].encode("utf-8")).hexdigest(),
        "command_limits": {
            "memory_max": "2G",
            "memory_swap_max": 0,
            "cpu_quota": "100%",
            "tasks_max": 128,
            "runtime_max_seconds": 240,
            "blas_threads": 1,
        },
    }
    OUTPUT.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
