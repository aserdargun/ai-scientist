"""Check the unmodified spec's Appendix C [3] values on production functions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from harness.alarm import apply_alarm_policy, nrm_task_score, pdm_task_score
from harness.contracts import AlarmPolicy
from harness.fingerprint import compute_harness_hash

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path(__file__).with_name("alarm-appendix-c-026-review.json")


def main() -> int:
    assert not OUTPUT.exists()
    fingerprint = compute_harness_hash(ROOT).sha256
    policy = AlarmPolicy(threshold=4.0, release=2.0, dwell=3)
    scores = np.zeros(200)
    scores[50:60] = 5.0
    on = apply_alarm_policy(scores, policy)
    masked = np.zeros(200, bool)
    masked[191:] = True
    good = np.zeros(200)
    good[120:191] = 5.0
    cases = (
        apply_alarm_policy(good, policy),
        np.ones(200, bool),
        apply_alarm_policy(np.maximum(scores, good), policy),
    )
    observed = tuple(pdm_task_score(v, [(100, 190)], masked, 600, max_fa=1.0) for v in cases)
    # Values come from the fixed Appendix C fixture, independently of production outputs.
    expected = ((68 / 90, 0.0, 0.0), (0.0, 1.44, 1.0), (0.0, 1.44, 0.08))
    normal = np.zeros(1440, bool)
    normal[500:503] = True
    normal_result = nrm_task_score(normal, np.zeros(1440, bool), 600)
    normal_expected = (float(np.exp(-0.1)), 0.1, 3 / 1440)
    always_on_normal = nrm_task_score(np.ones(1440, bool), np.zeros(1440, bool), 600)
    checks = {
        "dwell_and_release_exact": not bool(on[51]) and bool(on[52]) and bool(on[59]) and not bool(on[60]),
        "pdm_all_three_rows_float_hex_equal": all(float(a).hex() == float(b).hex() for row, wanted in zip(observed, expected, strict=True) for a, b in zip(row, wanted, strict=True)),
        "nrm_reference_float_hex_equal": all(float(a).hex() == float(b).hex() for a, b in zip(normal_result, normal_expected, strict=True)),
        "always_on_pdm_zero_not_rejected": observed[1][0] == 0.0,
        "always_on_nrm_zero_not_rejected": always_on_normal[0] == 0.0,
        "harness_unchanged": compute_harness_hash(ROOT).sha256 == fingerprint,
    }
    record = {
        "schema": "alarm-appendix-c-parity.v1",
        "source_commit": "55f85a0d45cad11164069cda85e7be2d317696bf",
        "harness_sha256": fingerprint,
        "spec_sha256": hashlib.sha256((ROOT / "docs/ai-scientist/01-ai-scientist-spec.md").read_bytes()).hexdigest(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "pdm_observed": observed,
        "pdm_expected": expected,
        "nrm_observed": normal_result,
        "nrm_expected": normal_expected,
        "nrm_always_on": always_on_normal,
        "checks": checks,
        "passed": all(checks.values()),
        "scope": "Production CPU functions, exact Appendix C [3] fixture. Not a Docker/Scorer, dataset or model measurement.",
    }
    OUTPUT.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"passed": record["passed"], "checks": checks}))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
