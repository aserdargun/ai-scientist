"""Write a twenty-turn fake-provider input; decisions come from real measurements."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from review_director_synthetic_scenario import (
    CONSTANT,
    CRASH,
    LONG_FIT,
    RESIDUAL_INVERSE,
    RESIDUAL_SIMPLE,
    RESIDUAL_VERBOSE,
)

from lab.director.contracts import CandidateProposal
from lab.director.fake_llm import ProposalTurn

ROOT = Path(__file__).resolve().parents[3]
_SCORE = "return np.abs(values[:, 1] - (self.slope * values[:, 0] + self.intercept))"


def turns() -> tuple[ProposalTurn, ...]:
    """Give no score, guard outcome or verdict to the deterministic provider."""
    nan_source = RESIDUAL_SIMPLE.replace(_SCORE, "return np.full(len(values), np.nan)")
    short_source = RESIDUAL_SIMPLE.replace(_SCORE, _SCORE + "[:-1]")
    unseeded_source = RESIDUAL_SIMPLE.replace(
        _SCORE, "return np.random.default_rng().normal(size=len(values))"
    )
    leaky_source = RESIDUAL_SIMPLE.replace(
        _SCORE, "return np.abs(values[:, 1] - np.mean(values[:, 1]))"
    )
    cases = (
        ("fit the normal sensor relation", RESIDUAL_VERBOSE, "features"),
        ("express the same residual with fewer statements", RESIDUAL_SIMPLE, "simplify"),
        ("reverse the residual score ordering", RESIDUAL_INVERSE, "detector"),
        ("return an all-zero score vector", CONSTANT, "detector"),
        ("raise during candidate fitting", CRASH, "detector"),
        ("sleep for 120 seconds during fitting", LONG_FIT, "detector"),
        ("repeat the reverse residual detector", RESIDUAL_INVERSE, "detector"),
        ("emit non-finite candidate scores", nan_source, "detector"),
        ("omit the last output sample", short_source, "detector"),
        ("use an unseeded score generator", unseeded_source, "detector"),
        ("use evaluation statistics in scoring", leaky_source, "preprocess"),
        ("repeat the all-zero score vector", CONSTANT, "detector"),
        ("retry the reverse residual ordering", RESIDUAL_INVERSE, "detector"),
        ("exercise another candidate fit exception", CRASH, "detector"),
        ("exercise another output shape mismatch", short_source, "detector"),
        ("re-evaluate the reverse residual detector", RESIDUAL_INVERSE, "detector"),
        ("exercise another constant output", CONSTANT, "detector"),
        ("exercise another non-finite output", nan_source, "detector"),
        ("recheck the reverse residual ordering", RESIDUAL_INVERSE, "detector"),
        ("finish with another evaluation-statistics proposal", leaky_source, "preprocess"),
    )
    result = []
    for ordinal, (hypothesis, source, move_type) in enumerate(cases, start=1):
        ast.parse(source)
        result.append(
            ProposalTurn(
                proposal=CandidateProposal(
                    hypothesis=f"Synthetic proposal {ordinal}: {hypothesis}.",
                    move_type=move_type,
                    candidate_source=source,
                    predicted_delta=0.1,
                ),
                messages=(
                    f"Synthetic candidate source for proposal {ordinal}; no measured output.",
                ),
                input_tokens=100,
                output_tokens=600,
            )
        )
    return tuple(result)


def main() -> None:
    proposals = turns()
    directory = ROOT / "data/runtime/director20-scenario"
    directory.mkdir(parents=True, exist_ok=True)
    document = {
        "schema": "review-fake-provider-input.v1",
        "scope": "Proposal inputs only; no experiment was executed by this preparation.",
        "proposals": [turn.model_dump(mode="json") for turn in proposals],
    }
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    path = directory / "proposals.json"
    path.write_bytes(payload)
    print(
        json.dumps(
            {
                "path": str(path.relative_to(ROOT)),
                "proposals": len(proposals),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "scope": document["scope"],
            }
        )
    )


if __name__ == "__main__":
    main()
