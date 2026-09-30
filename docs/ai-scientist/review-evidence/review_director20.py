"""Prepare isolated inputs and verify the production CLI's twenty-proposal run.

The review installs synthetic profiles/labels, supplies source-only fake turns,
and reads immutable receipts. Baseline/proposal execution belongs to the CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from review_director20_scenario import turns
from review_director_synthetic_scenario import fixture_tasks
from review_scorer_queue import ROOT, engine
from sqlalchemy import delete, insert, text

from harness.baselines import champion_noise_sd, normalize_task_score
from harness.fingerprint import compute_harness_hash
from harness.referee import decide
from lab.db.schema import dataset_labels, dataset_profiles, runs
from lab.director.artifacts import read_director_artifact, read_registered_calibration
from lab.director.contracts import ExperimentDocument, SourceProvenance, TrajectoryDocument
from lab.director.ledger import canonical_json_bytes
from lab.director.runner import _source_is_measurably_simpler
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def private_json(path: Path, value: dict) -> str:
    payload = canonical_json_bytes(value)
    path.write_bytes(payload)
    path.chmod(0o600)
    return digest(payload)


def prepare_inputs(run_id: UUID, runtime: Path) -> tuple[dict, list[dict], dict]:
    """Keep labels outside the CLI manifest and all proposal provider inputs."""
    runtime.mkdir(parents=True, mode=0o700)
    task_inputs, profiles = [], []
    for index, (train, evaluation, labels) in enumerate(fixture_tasks()):
        dataset_id = f"director20-{run_id}-{index}"
        profile_sha = digest(
            canonical_json_bytes(
                {
                    "dataset_id": dataset_id,
                    "columns": list(train.columns),
                    "train": train.to_numpy().tolist(),
                    "evaluation": evaluation.to_numpy().tolist(),
                    "labels": labels.tolist(),
                    "sampling_s": 60,
                    "sliding_window": 4,
                }
            )
        )
        identity = {
            "dataset_id": dataset_id,
            "split_id": "review-v1",
            "session_id": "local",
        }
        provenance = SourceProvenance(
            **identity,
            source_manifest_sha256=profile_sha,
            source_revision="synthetic-review-v1",
            license_id="CC0-1.0",
            attribution="Local generated review fixture",
            access_terms="Synthetic data generated for this review",
            usage_profile="noncommercial_research",
        )
        task_inputs.append(
            {
                **identity,
                "task_id": f"synthetic-task-{index}",
                "profile_sha256": profile_sha,
                "family": "EVT",
                "task_weight": 0.25,
                "provenance": provenance.model_dump(mode="json"),
                "context": {
                    "seed": 0,
                    "signals": list(train.columns),
                    "regime_signals": [],
                    "sampling_s": 60,
                    "time_budget_s": 90.0,
                },
                "columns": list(train.columns),
                "train": train.to_numpy().tolist(),
                "evaluation": evaluation.to_numpy().tolist(),
            }
        )
        profiles.append(
            {
                **identity,
                "profile_sha256": profile_sha,
                "sample_count": len(labels),
                "sliding_window": 4,
                "visibility": "dev",
                "labels": labels.tolist(),
            }
        )
    manifest = {
        "schema": "director-suite.v1",
        "suite_id": "synthetic.director20.v1",
        "suite_version": 1,
        "tasks": task_inputs,
    }
    scenario = {
        "schema": "review-fake-provider-input.v1",
        "scope": "Proposal inputs only; measurements and decisions must come from production.",
        "proposals": [turn.model_dump(mode="json") for turn in turns()],
    }
    inputs = {
        "suite_file": str(runtime / "suite.json"),
        "suite_manifest_sha256": private_json(runtime / "suite.json", manifest),
        "scenario_file": str(runtime / "proposals.json"),
        "scenario_sha256": private_json(runtime / "proposals.json", scenario),
    }
    return manifest, profiles, inputs


def install_fixture(migrator, run_id: UUID, manifest: dict, profiles: list[dict], inputs: dict):
    request = {
        "suite": manifest["suite_id"],
        "profile": "review-only",
        "suite_manifest_sha256": inputs["suite_manifest_sha256"],
        "scenario_sha256": inputs["scenario_sha256"],
        "proposal_limit": 20,
        "budget": {"experiments": 20, "wall_seconds": 14_400, "model_tokens": 350_000},
    }
    with migrator.begin() as connection:
        connection.execute(
            insert(runs).values(
                run_id=run_id,
                origin="local",
                owner_id=f"review:{run_id}",
                idempotency_key=str(run_id),
                payload_sha256=digest(canonical_json_bytes(request)),
                request_json=request,
                state="running",
            )
        )
        for profile in profiles:
            connection.execute(
                insert(dataset_profiles).values(
                    **{key: value for key, value in profile.items() if key != "labels"}
                )
            )
            connection.execute(
                insert(dataset_labels),
                [
                    {
                        "dataset_id": profile["dataset_id"],
                        "split_id": profile["split_id"],
                        "session_id": profile["session_id"],
                        "sample_index": index,
                        "is_anomaly": bool(label),
                    }
                    for index, label in enumerate(profile["labels"])
                ],
            )


def verify_scored_decisions(documents, scores, calibration, blob_root):
    """Rebuild measured decision inputs from Scorer rows and frozen calibration.

    This review check validates Director wiring. It is not the product replay CLI
    and does not independently rerun unmeasured guard rejection outcomes.
    """
    references = sorted(
        calibration.tasks,
        key=lambda task: (task.dataset_id, task.split_id, task.session_id, task.task_id),
    )
    weights = [task.task_weight for task in references]
    by_seed = {}
    for row in scores:
        key = (row["experiment_id"], row["seed"], row["task_id"])
        assert key not in by_seed
        by_seed[key] = float(row["vus_pr"])

    def normalized(experiment_id, seeds):
        return [
            float(
                normalize_task_score(
                    sum(by_seed[(experiment_id, seed, task.task_id)] for seed in seeds)
                    / len(seeds),
                    task.base_score,
                    task.reference_score,
                    family=task.family,
                )
            )
            for task in references
        ]

    def suite_score(vector):
        return sum(value * weight for value, weight in zip(vector, weights, strict=True)) / sum(
            weights
        )

    champion = next(
        pair["experiment"]
        for pair in documents
        if pair["experiment"]["experiment_id"] == calibration.champion_experiment_id
    )
    best = suite_score(normalized(champion["experiment_id"], (0, 1)))
    noise = calibration.champion_noise_sd
    trace = []
    for pair in documents:
        proposal = pair["experiment"]
        if proposal["kind"] != "proposal":
            continue
        assert proposal["parent_experiment_id"] == champion["experiment_id"]
        if proposal["status"] != "scored":
            assert proposal["decision"]["verdict"] == "REJECT"
            assert proposal["decision"]["delta"] is None
            assert proposal["decision"]["ci_low"] is None
            continue
        confirmed = any(key[0] == proposal["experiment_id"] and key[1] == 1 for key in by_seed)
        seeds = (0, 1) if confirmed else (0,)
        child = normalized(proposal["experiment_id"], seeds)
        parent = normalized(champion["experiment_id"], seeds)
        candidate_source = read_director_artifact(
            proposal["candidate_blob_sha256"], artifact_root=blob_root
        )
        champion_source = read_director_artifact(
            champion["candidate_blob_sha256"], artifact_root=blob_root
        )
        result = decide(
            parent,
            child,
            weights,
            eps=0.01,
            noise_sd=noise,
            simpler=_source_is_measurably_simpler(candidate_source, champion_source),
            guards_ok=all(value == "pass" for value in proposal["guards"].values()),
            best_suite=best,
            seed=0,
        )
        assert result.verdict == proposal["decision"]["verdict"]
        assert float(result.delta).hex() == float(proposal["decision"]["delta"]).hex()
        assert float(result.ci_low).hex() == float(proposal["decision"]["ci_low"]).hex()
        assert float(noise).hex() == float(proposal["decision"]["noise_sd"]).hex()
        assert suite_score(child).hex() == float(proposal["suite_score"]).hex()
        trace.append(
            {
                "experiment_id": proposal["experiment_id"],
                "decision_seeds": list(seeds),
                "parent": parent,
                "child": child,
                "best_before": best,
                "noise_before": noise,
                "verdict": result.verdict,
                "delta_hex": float(result.delta).hex(),
                "ci_low_hex": float(result.ci_low).hex(),
            }
        )
        if result.verdict in {"KEEP", "KEEP_SIMPLER"}:
            best = max(best, suite_score(child))
            noise = champion_noise_sd(
                {
                    seed: suite_score(normalized(proposal["experiment_id"], (seed,)))
                    for seed in (0, 1, 2)
                }
            )
            champion = proposal
    assert trace
    return trace


def verify_run(director, run_id: UUID, blob_root: Path) -> dict:
    """Use allowed receipts and actual bytes; never read protected record tables."""
    with director.connect() as connection:
        experiments = (
            connection.execute(
                text("SELECT * FROM lab.experiments WHERE run_id=:run ORDER BY sequence"),
                {"run": run_id},
            )
            .mappings()
            .all()
        )
        scores = (
            connection.execute(
                text(
                    "SELECT * FROM lab.dev_task_results WHERE run_id=:run "
                    "ORDER BY experiment_id,evaluation_kind,seed,task_id"
                ),
                {"run": run_id},
            )
            .mappings()
            .all()
        )
        report = dict(
            connection.execute(
                text("SELECT report_json,report_sha256 FROM lab.reports WHERE run_id=:run"),
                {"run": run_id},
            )
            .mappings()
            .one()
        )
        documents = []
        for row in experiments:
            receipt = connection.execute(
                text("SELECT lab.experiment_record_receipt(:id)"),
                {"id": row["experiment_id"]},
            ).scalar_one()
            pair = {}
            for name, model in (
                ("experiment", ExperimentDocument),
                ("trajectory", TrajectoryDocument),
            ):
                payload = read_director_artifact(
                    receipt[f"{name}_blob_sha256"], artifact_root=blob_root
                )
                assert digest(payload) == receipt[f"{name}_sha256"]
                document = model.model_validate_json(payload, strict=True)
                assert document.run_id == run_id and document.experiment_id == row["experiment_id"]
                pair[name] = document.model_dump(mode="json", by_alias=True)
            candidate = read_director_artifact(
                pair["experiment"]["candidate_blob_sha256"], artifact_root=blob_root
            )
            assert digest(candidate) == pair["experiment"]["candidate_sha256"]
            for key in ("messages_blob_sha256", "inputs_sha256"):
                value = pair["trajectory"][key]
                assert digest(read_director_artifact(value, artifact_root=blob_root)) == value
            assert pair["trajectory"]["outcome"] == pair["experiment"]["decision"]
            documents.append(pair)
    proposals = [
        pair["experiment"] for pair in documents if pair["experiment"]["kind"] == "proposal"
    ]
    baselines = [
        pair["experiment"] for pair in documents if pair["experiment"]["kind"] == "baseline"
    ]
    counts = Counter(item["decision"]["verdict"] for item in proposals)
    assert len(proposals) == 20 and len(baselines) == 3
    assert [item["experiment_number"] for item in proposals] == list(range(1, 21))
    assert [item["ordinal"] for item in proposals] == list(range(4, 24))
    assert set(counts) == {"KEEP", "KEEP_SIMPLER", "DISCARD", "REJECT"}
    assert any(item["status"] == "crashed" for item in proposals)
    assert any(item["decision"]["reason"] == "timeout" for item in proposals)
    assert digest(canonical_json_bytes(report["report_json"])) == report["report_sha256"]
    assert len(report["report_json"]["task_scores"]) == len(scores)
    baseline_ids = {item["experiment_id"] for item in baselines}
    baseline_scores = [row for row in scores if row["experiment_id"] in baseline_ids]
    assert len(baseline_scores) == 36
    champion = next(item for item in baselines if item["baseline_name"] == "robust_z")
    for proposal in proposals:
        assert proposal["parent_experiment_id"] == champion["experiment_id"]
        if proposal["decision"]["verdict"] in {"KEEP", "KEEP_SIMPLER"}:
            measured = [row for row in scores if row["experiment_id"] == proposal["experiment_id"]]
            assert len(measured) == 12
            assert {(row["evaluation_kind"], row["seed"]) for row in measured} == {
                ("primary", 0),
                ("confirmation", 1),
                ("confirmation", 2),
            }
            champion = proposal
    calibration = read_registered_calibration(director, run_id=run_id, artifact_root=blob_root)
    decision_trace = verify_scored_decisions(documents, scores, calibration, blob_root)
    return {
        "documents": documents,
        "scores": [dict(row) | {"run_id": str(run_id)} for row in scores],
        "report": report,
        "verdict_counts": dict(counts),
        "measured_decision_trace": decision_trace,
        "checks": {
            "twenty_proposals_three_baselines": True,
            "all_four_verdicts_measured": True,
            "candidate_crash_and_timeout": True,
            "terminal_pairs_and_physical_blobs": True,
            "baseline_36_measurements": True,
            "promotions_three_seeds_and_parent_lineage": True,
            "report_hash_and_score_count": True,
            "measured_decisions_delta_ci_and_prior_noise_bit_parity": True,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    run_id = uuid4()
    runtime = ROOT / "data/runtime/director20-review" / str(run_id)
    manifest, profiles, inputs = prepare_inputs(run_id, runtime)
    if args.prepare_only:
        print(json.dumps({"run_id": str(run_id), **inputs, "executed": False}), flush=True)
        return
    # The production CLI interface is required; never replace this with a review loop.
    command = [
        str(ROOT / ".venv/bin/python"),
        "-m",
        "lab.cli",
        "run",
        "--run-id",
        str(run_id),
        "--suite-id",
        manifest["suite_id"],
        "--suite-file",
        inputs["suite_file"],
        "--provider",
        "fake-json",
        "--scenario-file",
        inputs["scenario_file"],
        "--proposal-limit",
        "20",
        "--seed-wall-seconds",
        "90",
        "--artifact-root",
        str(runtime / "blobs"),
    ]
    initial_hash = compute_harness_hash(ROOT).sha256
    evidence = {
        "schema": "director20-cli-review.v1",
        "checked_at": datetime.now(UTC).isoformat(),
        "run_id": str(run_id),
        "scope": (
            "Production CLI baseline and 20 proposals on four synthetic EVT tasks. "
            "Fake provider supplies source only. Real Docker/Scorer/PostgreSQL required. "
            "No public data, real LLM, GPU, AOS, holdout or full replay acceptance."
        ),
        "harness_sha256": initial_hash,
        "image": DEFAULT_SANDBOX_IMAGE,
        "inputs": inputs,
        "command": command,
        "script_sha256": digest(Path(__file__).read_bytes()),
        "checks": {},
    }
    migrator, director = engine("migrator"), engine("director")
    try:
        install_fixture(migrator, run_id, manifest, profiles, inputs)
        print(json.dumps({"stage": "production_cli", "run_id": str(run_id)}), flush=True)
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
        evidence["process_exit_code"] = result.returncode
        (runtime / "cli.stdout").write_text(result.stdout)
        (runtime / "cli.stderr").write_text(result.stderr)
        evidence["stdout_path"] = str((runtime / "cli.stdout").relative_to(ROOT))
        evidence["stderr_path"] = str((runtime / "cli.stderr").relative_to(ROOT))
        assert result.returncode == 0, "production CLI did not exit successfully"
        evidence.update(verify_run(director, run_id, runtime / "blobs"))
    except Exception as error:
        evidence["error_type"] = type(error).__name__
        raise
    finally:
        with migrator.begin() as connection:
            evidence["final_ledger_snapshot"] = {
                "experiments": [
                    dict(row)
                    for row in connection.execute(
                        text(
                            "SELECT experiment_id,sequence,kind,status FROM lab.experiments "
                            "WHERE run_id=:run ORDER BY sequence"
                        ),
                        {"run": run_id},
                    ).mappings()
                ],
                "scored_tasks": connection.execute(
                    text("SELECT count(*) FROM lab.dev_task_results WHERE run_id=:run"),
                    {"run": run_id},
                ).scalar_one(),
            }
            live_jobs = connection.execute(
                text(
                    "SELECT count(*) FROM scorer.score_jobs WHERE run_id=:run AND state='running'"
                ),
                {"run": run_id},
            ).scalar_one()
            if live_jobs == 0:
                connection.execute(delete(runs).where(runs.c.run_id == run_id))
                for profile in profiles:
                    connection.execute(
                        delete(dataset_profiles).where(
                            dataset_profiles.c.dataset_id == profile["dataset_id"]
                        )
                    )
                evidence["owned_sql_fixtures_removed"] = True
            else:
                evidence["owned_sql_fixtures_removed"] = False
        migrator.dispose()
        director.dispose()
        evidence["source_unchanged"] = compute_harness_hash(ROOT).sha256 == initial_hash
        evidence["all_passed"] = (
            bool(evidence["checks"])
            and all(evidence["checks"].values())
            and evidence["source_unchanged"]
            and evidence.get("process_exit_code") == 0
            and evidence["owned_sql_fixtures_removed"]
            and "error_type" not in evidence
        )
        path = Path(__file__).with_name("director20-cli-review.json")
        path.write_text(json.dumps(evidence, indent=2) + "\n")
    assert evidence["all_passed"]
    print(json.dumps({"all_passed": True, "checks": evidence["checks"]}), flush=True)


if __name__ == "__main__":
    main()
