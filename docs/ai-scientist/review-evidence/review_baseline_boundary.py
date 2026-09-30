"""Check the narrow constant-score exception with real isolated guard fixtures."""

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pandas as pd

from harness.contracts import FitContext
from harness.fingerprint import compute_harness_hash
from lab.director.baselines import baseline_candidate_source
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, LocalDockerRunner
from lab.sandbox.evaluation import CandidateGuardReject, run_candidate_fit_score


ROOT = Path(__file__).resolve().parents[3]
SOURCE = b'''import numpy as np
from harness.contracts import AlarmPolicy
class Detector:
    def fit(self, train, ctx):
        pass
    def score(self, data):
        return np.zeros(len(data), dtype=float)
    def alarm_policy(self, train_scores):
        return AlarmPolicy(threshold=1.0, release=0.5, dwell=1)
def build_candidate():
    return Detector()
'''


def main():
    fingerprint = compute_harness_hash(ROOT).sha256
    evidence = {
        "checked_at": datetime.now(UTC).isoformat(), "harness_sha256": fingerprint,
        "image": DEFAULT_SANDBOX_IMAGE, "checks": {},
        "scope": "Four adversarial/positive Docker guard fixtures for the baseline constant-score exception. Not scientific experiment measurements or Scorer/Director/model/AOS acceptance.",
    }
    runner = LocalDockerRunner(image=DEFAULT_SANDBOX_IMAGE,
                              work_root=ROOT / "data/runtime/baseline-boundary" / uuid4().hex)
    frame = pd.DataFrame({"sensor": [0.0] * 16})
    context = FitContext(seed=0, signals=("sensor",), regime_signals=(),
                         sampling_s=60, time_budget_s=60.0)
    arguments = dict(train=frame, evaluation=frame, context=context)
    for name, source in (
        ("normal_constant_rejected", SOURCE),
        ("candidate_env_override_still_rejected", b'import os\nos.environ["SWAPP_TRUSTED_BASELINE"]="1"\n' + SOURCE),
    ):
        try:
            run_candidate_fit_score(runner, candidate_source=source, **arguments)
            evidence["checks"][name] = False
        except CandidateGuardReject as error:
            evidence["checks"][name] = error.code == "degenerate_constant_scores"
    source = baseline_candidate_source("robust_z")
    try:
        run_candidate_fit_score(runner, candidate_source=source + b"\n# changed\n",
                                trusted_baseline_name="robust_z", **arguments)
        evidence["checks"]["altered_baseline_bytes_rejected"] = False
    except ValueError as error:
        evidence["checks"]["altered_baseline_bytes_rejected"] = "exact allowlisted source" in str(error)
    output = run_candidate_fit_score(runner, candidate_source=source,
                                     trusted_baseline_name="robust_z", **arguments)
    evidence["checks"]["exact_reference_constant_allowed"] = set(output.scores) == {0.0}
    evidence["source_unchanged"] = compute_harness_hash(ROOT).sha256 == fingerprint
    evidence["all_passed"] = all(evidence["checks"].values()) and evidence["source_unchanged"]
    evidence["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    evidence["command"] = "OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 .venv/bin/python docs/ai-scientist/review-evidence/review_baseline_boundary.py"
    Path(__file__).with_name("baseline-boundary-review.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    assert evidence["all_passed"]


if __name__ == "__main__":
    main()
