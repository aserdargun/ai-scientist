"""Compare current Referee with the six exact Appendix C [2] cases."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import types
from pathlib import Path

import numpy as np

from harness.fingerprint import compute_harness_hash
from harness.referee import decide

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).with_name("referee-appendix-c-027-v2-review.json")


def main() -> None:
    assert not OUT.exists()
    spec_path = ROOT / "docs/ai-scientist/01-ai-scientist-spec.md"
    source_path = ROOT / "harness/referee.py"
    hashes = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (spec_path, source_path, Path(__file__))
    }
    appendix = spec_path.read_text().split("## Ek C — Referans kod (test edilmiş)", 1)[
        1
    ]
    block = re.findall(r"```python\n(.*?)\n```", appendix, re.S)[0]
    reference = types.ModuleType("appendix_c_frozen_reference")
    sys.modules[reference.__name__] = reference
    exec(compile(block, "spec-appendix-c-reference", "exec"), reference.__dict__)
    rng = np.random.default_rng(0)
    # Preserve the exact RNG position after Appendix C's train/eval creation.
    rng.normal(size=(3000, 3))
    rng.normal(size=(2000, 3))
    parent = rng.uniform(0.3, 0.9, 24)
    weights = np.ones(24)
    defaults = dict(eps=0.01, noise_sd=0.004, guards_ok=True)
    cases = [
        (parent + rng.normal(0.03, 0.02, 24), dict(simpler=False, **defaults)),
        (parent + rng.normal(0.00, 0.03, 24), dict(simpler=False, **defaults)),
        (parent + rng.normal(-0.002, 0.004, 24), dict(simpler=True, **defaults)),
        (parent + 0.2, dict(simpler=False, eps=0.01, noise_sd=0.004, guards_ok=False)),
    ]
    large_drop = parent + 0.05
    large_drop[3] -= 0.8
    cases.append((large_drop, dict(simpler=False, **defaults)))
    cases.append(
        (
            parent + rng.normal(-0.002, 0.004, 24),
            dict(simpler=True, best_suite=float(parent.mean()) + 0.05, **defaults),
        )
    )
    expected = ["KEEP", "DISCARD", "KEEP_SIMPLER", "REJECT", "DISCARD", "DISCARD"]
    rows = []
    for index, ((child, kwargs), verdict) in enumerate(
        zip(cases, expected, strict=True), 1
    ):
        original = reference.decide(parent, child, weights, **kwargs)
        current = decide(parent, child, weights, **kwargs)
        assert original.verdict == current.verdict == verdict
        fields = {}
        for name in ("delta", "ci_low"):
            before, after = getattr(original, name), getattr(current, name)
            if verdict == "REJECT":
                assert math.isnan(before) and after is None
                fields[name] = {
                    "original": "NaN",
                    "current": None,
                    "review_json_fix": True,
                }
            else:
                assert math.isfinite(before) and math.isfinite(after)
                assert round(before, 4) == round(after, 4)
                fields[name] = {
                    "reference_hex": float(before).hex(),
                    "production_hex": float(after).hex(),
                    "absolute_difference": abs(float(before) - float(after)),
                }
        rows.append(
            {"case": index, "verdict": verdict, "values": fields, "passed": True}
        )
    unchanged = all(
        hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == value
        for name, value in hashes.items()
    )
    assert unchanged
    result = {
        "schema": "referee-appendix-c-review.v1",
        "checks": rows,
        "passed": True,
        "source_sha256": hashes,
        "source_unchanged": unchanged,
        "harness_sha256": compute_harness_hash(ROOT).sha256,
        "scope": "Exact Appendix C [2] deterministic fixtures. Six required verdict assertions and the printed four-decimal finite outputs match; guard REJECT uses the reviewed strict-JSON null replacement. Replay bit parity is a separate production-run check. No generalization or public/model acceptance.",
    }
    OUT.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"passed_cases": len(rows), "source_unchanged": unchanged}))


if __name__ == "__main__":
    main()
