"""Review candidate-domain errors before infrastructure retry classification."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
from types import ModuleType

from lab.scorer.service import CandidateOutputError, parse_candidate_score

ROOT = Path(__file__).resolve().parents[3]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", choices=("before", "fixed"), required=True)
    args = parser.parse_args()
    source = ROOT / "lab/scorer/service.py"
    source_bytes = source.read_bytes()
    parse = parse_candidate_score
    baseline_commit = None
    if args.label == "before":
        # A fixed trusted repository revision avoids observing a partially edited
        # working file while the implementation agent applies the correction.
        baseline_commit = "602aecd"
        source_bytes = subprocess.run(
            ["git", "show", baseline_commit + ":lab/scorer/service.py"], cwd=ROOT,
            capture_output=True, check=True,
        ).stdout
        module = ModuleType("owned_parser_baseline_review")
        sys.modules[module.__name__] = module
        exec(compile(source_bytes, "git:602aecd:lab/scorer/service.py", "exec"), module.__dict__)
        parse = module.parse_candidate_score
    digest = hashlib.sha256(source_bytes).hexdigest()
    cases = (
        ("positive_exponent_overflow", b'{"schema":"candidate-scores.v1","sample_indices":[0,1],"scores":[0.0,1e999]}', False),
        ("negative_exponent_overflow", b'{"schema":"candidate-scores.v1","sample_indices":[0,1],"scores":[0.0,-1e999]}', False),
        ("unequal_parallel_arrays", json.dumps({"schema": "candidate-scores.v1", "sample_indices": list(range(16)), "scores": [0.0, 1.0]}).encode(), False),
        ("valid_finite_control", b'{"schema":"candidate-scores.v1","sample_indices":[0,1],"scores":[0.0,1.0]}', True),
    )
    results = []
    for name, payload, should_accept in cases:
        result = {"case": name, "payload": payload.decode(), "should_accept": should_accept}
        try:
            parsed = parse(payload)
            result.update(accepted=True, all_scores_finite=all(math.isfinite(x) for x in parsed.scores),
                          index_count=len(parsed.sample_indices), score_count=len(parsed.scores))
        except CandidateOutputError:
            result.update(accepted=False, typed_candidate_error=True)
        except Exception as error:
            result.update(accepted=False, typed_candidate_error=False, error_type=type(error).__name__)
        result["passed"] = result["accepted"] == should_accept and (
            should_accept or result.get("typed_candidate_error") is True
        )
        results.append(result)
    record = {
        "checked_at": datetime.now(UTC).isoformat(), "cases": results,
        "scope": "Production numeric artifact parser only; verifies malformed numeric output is typed as candidate error before VUS/worker infrastructure handling. No DB, worker, sandbox or model acceptance.",
        "source_sha256": {"lab/scorer/service.py": digest},
        "source_unchanged": baseline_commit is not None or digest == hashlib.sha256(source.read_bytes()).hexdigest(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    if baseline_commit is not None:
        record["baseline_commit"] = baseline_commit
    record["all_passed"] = all(row["passed"] for row in results) and record["source_unchanged"]
    record["violation_reproduced"] = args.label == "before" and not record["all_passed"]
    Path(__file__).with_name(f"candidate-score-parser-{args.label}.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    if args.label == "fixed" and not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
