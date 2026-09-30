"""Preflight real synthetic candidate behavior, separate from Director acceptance."""

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4

from harness.contracts import FitContext
from harness.metrics import vus_metrics
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, LocalDockerRunner
from lab.sandbox.evaluation import CandidateGuardReject, run_guarded_seed_evaluation
from review_director_synthetic_scenario import (
    ROOT, RESIDUAL_VERBOSE, RESIDUAL_SIMPLE, RESIDUAL_INVERSE, CONSTANT, CRASH,
    LONG_FIT, fixture_tasks,
)


def main() -> None:
    paths = [Path(__file__), Path(__file__).with_name("review_director_synthetic_scenario.py")]
    paths.extend(ROOT / name for name in (
        "harness/contracts.py", "harness/metrics.py", "lab/sandbox/evaluation.py",
        "lab/sandbox/docker_runner.py", "lab/sandbox/candidate_entrypoint.py", "ops/sandbox-image.lock",
    ))
    hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    record = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Six synthetic candidate sources, one tiny EVT task, real networkless CPU Docker fit/score/guards. Host computes fixture metrics only. Previous pinned image reused; this is not whole-harness parity, independent Scorer, Director20, public data, LLM, GPU or AOS acceptance.",
        "image": DEFAULT_SANDBOX_IMAGE, "source_sha256": hashes, "cases": [], "checks": {},
    }
    runtime = ROOT / "data/runtime/director-scenario-preflight" / uuid4().hex
    runner = LocalDockerRunner(image=DEFAULT_SANDBOX_IMAGE, work_root=runtime)
    train, evaluation, labels = fixture_tasks()[0]
    for name, source, expected in (
        ("verbose", RESIDUAL_VERBOSE, "pass"),
        ("simple", RESIDUAL_SIMPLE, "pass"),
        ("inverse", RESIDUAL_INVERSE, "pass"),
        ("constant", CONSTANT, "degenerate_constant_scores"),
        ("crash", CRASH, "candidate_crash"),
        ("long_fit", LONG_FIT, "timeout"),
    ):
        started = time.monotonic()
        row = {"name": name, "expected": expected, "candidate_sha256": hashlib.sha256(source.encode()).hexdigest()}
        try:
            output = run_guarded_seed_evaluation(
                runner, candidate_source=source.encode(), train=train, evaluation=evaluation,
                context=FitContext(0, tuple(train.columns), (), 60, 60.0),
                task_ids=frozenset(f"synthetic-correlated-{i}" for i in range(4)),
                evaluation_instants=frozenset(), remaining_seconds=3 if name == "long_fit" else 90,
            )
            row.update(observed="pass", vus_pr=vus_metrics(labels, output.evaluation.scores, sliding_window=4).vus_pr,
                       score_sha256=hashlib.sha256(output.evaluation.score_document).hexdigest(),
                       fit_container=output.evaluation.fit_container_name,
                       score_container=output.evaluation.score_container_name)
        except CandidateGuardReject as error:
            row["observed"] = error.code
        row["wall_seconds"] = time.monotonic() - started
        row["passed"] = row["observed"] == expected
        record["cases"].append(row)
        print(json.dumps(row), flush=True)
    record["checks"] = {
        "all_six_candidate_behaviors_match": all(row["passed"] for row in record["cases"]),
        "verbose_and_simple_identical_scores": record["cases"][0].get("score_sha256") is not None and record["cases"][0].get("score_sha256") == record["cases"][1].get("score_sha256"),
        "inverse_has_lower_vus": record["cases"][2].get("vus_pr", 1) < record["cases"][0].get("vus_pr", 0),
        "long_fit_is_bounded": record["cases"][5]["wall_seconds"] < 15.0,
    }
    record["source_unchanged"] = all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest for path, digest in hashes.items())
    record["all_passed"] = all(record["checks"].values()) and record["source_unchanged"]
    record["command"] = ".venv/bin/python docs/ai-scientist/review-evidence/review_director_scenario_docker.py"
    Path(__file__).with_name("director-scenario-docker-review.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"checks": record["checks"], "source_unchanged": record["source_unchanged"], "all_passed": record["all_passed"]}, indent=2))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
