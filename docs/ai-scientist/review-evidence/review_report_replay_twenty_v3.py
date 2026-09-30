"""Exercise report/replay CLIs on twenty real guarded synthetic proposals.

Run only against the dedicated frozen 0.27 worktree and private database.
The historical scenario supplies candidate source, never measured outcomes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

REPOSITORY = Path(__file__).resolve().parents[3]
WORKTREE = REPOSITORY / "data/runtime/parallel-m0/report-replay-027"
EVIDENCE = REPOSITORY / "docs/ai-scientist/review-evidence"
OUTPUT = EVIDENCE / "report-replay-twenty-027-v3-review.json"
EXPECTED_COMMIT = "390a6c25e94e5da339457dd17e88749ed07c7795"
EXPECTED_HARNESS = "0c96264015cf32f8be9f7d4ef1de0572987203e97b1d044f69e9af9af780cb71"

# Resolve both production modules and the unchanged review helpers in the clone.
sys.path.insert(0, str(WORKTREE / "docs/ai-scientist/review-evidence"))
sys.path.insert(0, str(WORKTREE))

from review_director20 import (
    install_fixture,
    prepare_inputs as prepare_legacy_inputs,
    verify_run,
)
from review_scorer_queue import engine
from sqlalchemy import text

from harness.fingerprint import compute_harness_hash
from lab.director.ledger import canonical_json_bytes
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, PROJECT_ROOT
from lab.scorer.supervisor import PROJECT_ROOT as SCORER_ROOT


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def prepare_inputs(run_id, runtime: Path):
    """Add current weight provenance without changing any synthetic data or proposal."""
    from lab.director.suite_weights import (
        SuiteWeightInput,
        suite_weight_task_key,
        suite_weights,
    )

    manifest, profiles, inputs = prepare_legacy_inputs(run_id, runtime)
    for index, task in enumerate(manifest["tasks"]):
        task["independent_family"] = f"synthetic.sensor-relation-{index}"
        task["label_tier"] = "gold"
    records = tuple(
        SuiteWeightInput(
            task_id=suite_weight_task_key(
                (
                    task["dataset_id"],
                    task["split_id"],
                    task["session_id"],
                    task["task_id"],
                )
            ),
            task_type=task["family"],
            label_tier=task["label_tier"],
            family=task["independent_family"],
        )
        for task in manifest["tasks"]
    )
    weights = suite_weights(records)
    assert tuple(task["task_weight"] for task in manifest["tasks"]) == weights.weights
    manifest.update(
        weight_policy="spec-3.2.5-appendix-c-waterfill.v1",
        family_cap=weights.family_cap,
        type_shares=dict(weights.type_shares),
        family_shares=dict(weights.family_shares),
    )
    payload = canonical_json_bytes(manifest)
    Path(inputs["suite_file"]).write_bytes(payload)
    inputs["suite_manifest_sha256"] = sha(payload)
    return manifest, profiles, inputs


def command(arguments: list[str], runtime: Path, name: str, timeout: int) -> dict:
    environment = dict(os.environ, PYTHONPATH=str(WORKTREE))
    start = time.monotonic()
    result = subprocess.run(
        [str(WORKTREE / ".venv/bin/python"), "-m", "lab.cli", *arguments],
        cwd=WORKTREE,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    logs = {}
    for stream in ("stdout", "stderr"):
        path = runtime / f"{name}-v3.{stream}"
        path.write_text(getattr(result, stream))
        path.chmod(0o600)
        logs[stream] = {
            "path": str(path.relative_to(REPOSITORY)),
            "sha256": sha(path.read_bytes()),
        }
    record = {
        "arguments": arguments,
        "actual_exit_code": result.returncode,
        "elapsed_seconds": time.monotonic() - start,
        "logs": logs,
    }
    if result.returncode == 0:
        record["result"] = json.loads(result.stdout)
    return record


def same_float(left, right) -> bool:
    return (
        left is right
        if left is None or right is None
        else float(left).hex() == float(right).hex()
    )


def verify_cli_results(director, run_id, runtime: Path, review: dict) -> dict:
    blob_root = runtime / "blobs"
    report = command(
        ["report", str(run_id), "--artifact-root", str(blob_root)],
        runtime,
        "report",
        45,
    )
    replay = command(
        ["replay", str(run_id), "--artifact-root", str(blob_root)],
        runtime,
        "replay",
        45,
    )
    result = {"report": report, "replay": replay}
    assert report["actual_exit_code"] == replay["actual_exit_code"] == 0
    report_result = report["result"]
    report_path = Path(report_result["report"])
    assert report_path.parent == WORKTREE / "data/runtime/reports"
    html_bytes = report_path.read_bytes()
    assert sha(html_bytes) == report_result["sha256"]
    proposals = [
        pair for pair in review["documents"] if pair["experiment"]["kind"] == "proposal"
    ]
    counts = Counter(pair["experiment"]["decision"]["verdict"] for pair in proposals)
    assert report_result["summary"] == {
        "run_id": str(run_id),
        "experiment_count": 20,
        "baseline_count": 3,
        "keep_count": counts["KEEP"],
        "keep_simpler_count": counts["KEEP_SIMPLER"],
        "discard_count": counts["DISCARD"],
        "reject_count": counts["REJECT"],
    }
    # Compare chart coordinates' explicit sequence/score titles to ledger history.
    with director.connect() as connection:
        sequences = dict(
            connection.execute(
                text(
                    "SELECT experiment_id,sequence FROM lab.experiments WHERE run_id=:run"
                ),
                {"run": run_id},
            ).all()
        )
    from lab.director.artifacts import read_registered_calibration

    calibration = read_registered_calibration(director, run_id=run_id, artifact_root=blob_root)
    assert calibration.champion_seed_scores is not None
    baseline_score = sum(calibration.champion_seed_scores) / 3.0
    expected_points = []
    current = None
    for pair in review["documents"]:
        doc = pair["experiment"]
        if doc["kind"] == "baseline" and doc["baseline_name"] == "robust_z":
            current = baseline_score
            assert current is not None
            expected_points.append((sequences[doc["experiment_id"]], current))
        elif doc["kind"] == "proposal":
            if doc["decision"]["verdict"] in {"KEEP", "KEEP_SIMPLER"}:
                current = doc["suite_score"]
            assert current is not None
            expected_points.append((sequences[doc["experiment_id"]], current))
    html_text = html_bytes.decode()
    actual_points = re.findall(
        r"<title>Sequence ([0-9]+): score ([^<]+)</title>", html_text
    )
    assert len(actual_points) == len(expected_points) == 21
    assert all(
        int(sequence) == expected[0] and same_float(float(score), expected[1])
        for (sequence, score), expected in zip(
            actual_points, expected_points, strict=True
        )
    )
    path_match = re.search(r'<path d="([^"]+)" class="staircase"', html_text)
    assert path_match and path_match[1].count(" H ") == path_match[1].count(" V ") == 20

    expected = {}
    for pair in proposals:
        trajectory = pair["trajectory"]
        for manifest in trajectory["replay_manifests"]:
            expected[
                (pair["experiment"]["experiment_id"], manifest["decision_stage"])
            ] = manifest["expected_decision"]
        terminal = trajectory["terminal_replay"]
        if terminal is not None:
            expected[(pair["experiment"]["experiment_id"], "terminal")] = terminal[
                "decision"
            ]
    replay_rows = replay["result"]["replayed_decisions"]
    actual = {(row["experiment_id"], row["stage"]): row for row in replay_rows}
    assert len(actual) == len(replay_rows) and set(actual) == set(expected)
    for key, decision in expected.items():
        row = actual[key]
        assert row["verdict"] == decision["verdict"]
        assert all(
            same_float(row[name], decision[name])
            for name in ("delta", "ci_low", "noise_sd")
        )
        if key[1] == "terminal":
            assert row["verdict"] == "REJECT" and row["delta"] is row["ci_low"] is None
    result["replay_stage_counts"] = dict(Counter(row["stage"] for row in replay_rows))
    result["replayed_records"] = len(replay_rows)
    result["scoreless_rejection_scope"] = (
        "Immutable terminal dispositions verified; candidate guards are not rerun by replay."
    )
    result["chart_points"] = expected_points
    html_archive = EVIDENCE / "report-replay-twenty-027-v3.html"
    assert not html_archive.exists()
    html_archive.write_bytes(html_bytes)
    result["html_archive_sha256"] = sha(html_bytes)
    result["checks"] = {
        "actual_report_and_replay_cli_exit_zero": True,
        "html_hash_and_exact_ledger_counts": True,
        "all_twenty_staircase_steps_match_ledger": True,
        "every_immutable_replay_stage_present": True,
        "verdict_delta_ci_low_noise_bit_parity": True,
        "scoreless_rejections_preserve_null_measurements": True,
    }
    return result


def main() -> None:
    os.umask(0o077)
    assert not OUTPUT.exists()
    assert PROJECT_ROOT == SCORER_ROOT == WORKTREE
    original_path = EVIDENCE / "report-replay-twenty-027-v2-review.json"
    original = json.loads(original_path.read_text())
    assert original["execution"]["actual_exit_code"] == 0
    assert all(original["checks"].values())
    run_id = UUID(original["run_id"])
    runtime = WORKTREE / "data/runtime/report-replay-twenty" / str(run_id)
    initial_hash = compute_harness_hash(WORKTREE).sha256
    assert initial_hash == EXPECTED_HARNESS
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=WORKTREE, text=True).strip() == EXPECTED_COMMIT
    record = {
        "schema": "report-replay-existing-run-review.v1",
        "started_at": datetime.now(UTC).isoformat(),
        "run_id": str(run_id),
        "source_commit": EXPECTED_COMMIT,
        "harness_sha256": initial_hash,
        "driver_sha256": sha(Path(__file__).read_bytes()),
        "original_run_evidence_sha256": sha(original_path.read_bytes()),
        "scope": "Verify retained real Docker/Scorer 20-proposal run and rerun report/replay CLIs only. Synthetic source-only provider; no new experiments, public/model/GPU/holdout/AOS acceptance.",
        "checks": {},
    }
    director = engine("director")
    migrator = engine("migrator")
    started = time.monotonic()
    try:
        with director.connect() as connection:
            assert connection.execute(text("SELECT current_database()")).scalar_one() == "swapp_lab_m0_report_027_1e946abd"
        review = verify_run(director, run_id, runtime / "blobs")
        record["checks"].update(review["checks"])
        record["verdict_counts"] = review["verdict_counts"]
        record["score_count"] = len(review["scores"])
        cli = verify_cli_results(director, run_id, runtime, review)
        record["cli"] = cli
        record["checks"].update(cli["checks"])
        with migrator.connect() as connection:
            active = connection.execute(text("SELECT count(*) FROM scorer.score_jobs WHERE run_id=:run AND state IN ('queued','running')"), {"run": run_id}).scalar_one()
        assert active == 0
        record["checks"]["no_active_owned_score_jobs"] = True
    except BaseException as error:
        record["error_type"] = type(error).__name__
        raise
    finally:
        record["elapsed_seconds"] = time.monotonic() - started
        record["source_unchanged"] = compute_harness_hash(WORKTREE).sha256 == initial_hash
        record["all_passed"] = bool(record["checks"]) and all(record["checks"].values()) and record["source_unchanged"] and "error_type" not in record
        director.dispose()
        migrator.dispose()
        OUTPUT.write_text(json.dumps(record, indent=2) + "\n")
    assert record["all_passed"]
    print(json.dumps({key: record[key] for key in ("all_passed", "checks", "verdict_counts", "score_count", "elapsed_seconds")}))


if __name__ == "__main__":
    main()
