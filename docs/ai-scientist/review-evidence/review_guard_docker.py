"""Execute independent guard candidates only through the production Docker path."""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
import uuid

import numpy as np
import pandas as pd

from guard_review_candidates import cases
from harness.contracts import FitContext
from harness.fingerprint import compute_harness_hash
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, LocalDockerRunner, SandboxProfile
from lab.sandbox.evaluation import (
    CandidateGuardReject, check_complete_suite_position_bias, run_guarded_seed_evaluation,
)

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
EVIDENCE = HERE / "guard-docker-review.json"


def sources():
    paths = [
        ROOT / "harness/contracts.py", ROOT / "harness/fingerprint.py", ROOT / "harness/VERSION",
        ROOT / "Dockerfile.sandbox", ROOT / "docker/sandbox/requirements.txt",
        ROOT / "lab/scorer/service.py",
    ]
    paths += list((ROOT / "lab/sandbox").glob("*.py"))
    paths += list((ROOT / "lab/sandbox").glob("*.lock"))
    paths += list((ROOT / "ops").glob("sandbox-image.lock"))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


class RecordedRunner(LocalDockerRunner):
    """Record host-side phase identity; never deserialize the opaque model."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.phases = []

    def run_phase(self, **kwargs):
        record = {
            "phase": kwargs["phase"],
            "source_sha256": hashlib.sha256(kwargs["candidate_source"]).hexdigest(),
            "input_sha256": hashlib.sha256(kwargs["arrow_input"]).hexdigest(),
            "restored_fit_sha256": (hashlib.sha256(kwargs["fit_artifact"]).hexdigest()
                                    if kwargs.get("fit_artifact") is not None else None),
            "timeout_seconds": kwargs.get("timeout_seconds"),
        }
        self.phases.append(record)
        try:
            result = super().run_phase(**kwargs)
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            record["exit_code"] = getattr(exc, "exit_code", None)
            raise
        record.update({
            "container_name": result.container_name,
            "elapsed_seconds": result.elapsed_seconds,
            "exit_code": result.exit_code,
            "artifacts": [{"name": artifact.name, "sha256": artifact.sha256,
                           "bytes": artifact.size_bytes} for artifact in result.artifacts],
        })
        return result


def public_result(result):
    value = asdict(result)
    if isinstance(value.get("value"), float) and not math.isfinite(value["value"]):
        value["value"] = "nonfinite-relative-difference"
    return value


def main():
    before = sources()
    fingerprint = compute_harness_hash(ROOT).sha256
    nonce = uuid.uuid4().hex
    work_root = ROOT / "data/runtime" / ("owned-guard-review-" + nonce)
    profile = SandboxProfile(memory_bytes=512 * 1024**2, cpus=1.0, pids=64,
                             timeout_seconds=90, output_bytes=4 * 1024**2,
                             log_bytes=64 * 1024)
    runner = RecordedRunner(image=DEFAULT_SANDBOX_IMAGE, work_root=work_root, profile=profile)
    rng = np.random.default_rng(39841)
    train = pd.DataFrame(rng.normal(size=(192, 2)), columns=["sensor_a", "sensor_b"])
    evaluation = pd.DataFrame(rng.normal(size=(256, 2)), columns=train.columns)
    context = FitContext(seed=17, signals=tuple(train.columns), regime_signals=(),
                         sampling_s=60, time_budget_s=180.0)
    record = {
        "schema": "guard-docker-review.v1", "checked_at": datetime.now(UTC).isoformat(),
        "scope": "19 independent synthetic candidates through production static and real Docker guards; no private labels, real datasets, GPU, Scorer commit, Director, or AOS coexistence acceptance.",
        "image": DEFAULT_SANDBOX_IMAGE, "profile": asdict(profile),
        "harness_sha256": fingerprint, "source_sha256": before,
        "fixture": {"seed": 39841, "train_rows": len(train), "eval_rows": len(evaluation),
                    "columns": list(train.columns), "fit_context": asdict(context)},
        "cases": [], "passed": False,
        "command": ".venv/bin/python docs/ai-scientist/review-evidence/review_guard_docker.py",
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "candidate_catalog_script_sha256": hashlib.sha256((HERE / "guard_review_candidates.py").read_bytes()).hexdigest(),
    }
    relative_cgroup = Path("/proc/self/cgroup").read_text().strip().split("0::", 1)[1]
    cgroup = Path("/sys/fs/cgroup") / relative_cgroup.lstrip("/")
    record["host_review_process"] = {
        "pid": os.getpid(), "cgroup": relative_cgroup,
        "limits": {name: (cgroup / name).read_text().strip()
                   for name in ("memory.max", "memory.swap.max", "cpu.max", "pids.max")},
    }
    start = time.monotonic()
    try:
        for case in cases():
            phase_start = len(runner.phases)
            case_start = time.monotonic()
            item = {"name": case.name, "source_sha256": hashlib.sha256(case.source).hexdigest(),
                    "expected_rejection": list(case.expected_rejection), "guards": []}
            outcome = "pass"
            try:
                guarded = run_guarded_seed_evaluation(
                    runner, candidate_source=case.source, train=train, evaluation=evaluation,
                    context=context, task_ids=frozenset({"private-review-task-id"}),
                    evaluation_instants=frozenset(),
                    evaluation_intervals=(("2026-09-24T10:00:00Z", "2026-09-24T11:00:00Z"),),
                    remaining_seconds=2 if case.name == "slow_fit" else 180,
                )
                evaluated = guarded.evaluation
                item["initial_output"] = {
                    "score_rows": len(evaluated.scores), "policy": asdict(evaluated.policy),
                    "fit_artifact_sha256": evaluated.fit_artifact_sha256,
                    "score_document_sha256": hashlib.sha256(evaluated.score_document).hexdigest(),
                }
                item["guards"].extend(public_result(result) for result in (
                    guarded.hardcoding, guarded.determinism, guarded.causality,
                ))
                position = check_complete_suite_position_bias(
                    ((case.nrm_only, evaluated.scores),), expected_nrm_tasks=int(case.nrm_only),
                )
                item["guards"].append(public_result(position))
                if not position.passed:
                    outcome = position.code
            except CandidateGuardReject as exc:
                outcome = exc.code
            except Exception as exc:
                outcome = "unexpected_exception:" + type(exc).__name__
            item["outcome"] = outcome
            category = {"non_finite_scores": "degenerate",
                        "score_length_or_index_mismatch": "interface"}.get(outcome, outcome)
            item["rejection_category"] = category
            expected = case.expected_rejection
            item["passed"] = (outcome == "pass" if not expected else
                              any(category == code or category.startswith(code + "_") for code in expected))
            item["phases"] = runner.phases[phase_start:]
            fit_indices = [index for index, phase in enumerate(item["phases"]) if phase["phase"] == "fit"]
            if len(fit_indices) == 4:
                # Initial fit, two full determinism fits, then the causal fit.
                causal_phases = item["phases"][fit_indices[-1]:]
                restored = [phase["restored_fit_sha256"] for phase in causal_phases if phase["phase"] == "score"]
                item["causality_same_frozen_fit_for_all_scores"] = len(restored) == 4 and len(set(restored)) == 1
            names = [phase["container_name"] for phase in item["phases"] if "container_name" in phase]
            item["fresh_container_per_successful_phase"] = len(set(names)) == len(names)
            item["elapsed_seconds"] = time.monotonic() - case_start
            if item.get("causality_same_frozen_fit_for_all_scores") is False:
                item["passed"] = False
            item["passed"] &= item["fresh_container_per_successful_phase"]
            record["cases"].append(item)
            EVIDENCE.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
            print(json.dumps({key: item[key] for key in ("name", "outcome", "passed", "elapsed_seconds")}), flush=True)
    finally:
        record["elapsed_seconds"] = time.monotonic() - start
        record["source_unchanged"] = before == sources()
        record["harness_unchanged"] = fingerprint == compute_harness_hash(ROOT).sha256
        record["work_root_empty"] = not any(work_root.iterdir())
        live = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"],
                              capture_output=True, text=True, check=True).stdout.splitlines()
        names = {phase["container_name"] for phase in runner.phases if "container_name" in phase}
        record["successful_containers_removed"] = not names.intersection(live)
        if record["work_root_empty"]:
            work_root.rmdir()
        record["work_root_removed"] = not work_root.exists()
        record["phase_attempts"] = len(runner.phases)
        record["passed"] = (len(record["cases"]) == len(cases()) and
                            all(item["passed"] for item in record["cases"]) and
                            all(record[key] for key in ("source_unchanged", "harness_unchanged",
                                "work_root_empty", "work_root_removed", "successful_containers_removed")))
        EVIDENCE.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"passed": record["passed"], "cases": len(record["cases"]),
                      "phase_attempts": record["phase_attempts"], "elapsed_seconds": record["elapsed_seconds"]}))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
