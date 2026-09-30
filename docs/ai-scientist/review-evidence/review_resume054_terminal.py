"""Root-invoked, bounded read-only verification; never dispatches or resumes work."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from lab.director.artifacts import read_director_artifact
from lab.director.journal import canonical_bytes
from lab.reporting import read_run_pairs

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RUN = "c8709872-93db-449a-8aab-13aa0c8c44a2"
RESTART = "45a0ccc6-4dce-4649-95d3-545393cac388"
CELL = ("experiment_id", "evaluation_kind", "task_id", "seed")


def normalized(value):
    return json.loads(json.dumps(value, default=str, allow_nan=False))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load(name):
    return json.loads((HERE / name).read_text())


def stamp(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def cell(row):
    return tuple(row[key] for key in CELL)


def timeout(_signum, _frame):
    raise TimeoutError("verification exceeded90seconds")


def main():
    os.umask(0o077)
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(90)
    spec = importlib.util.spec_from_file_location("resume054_probe_readonly", HERE / "probe.py")
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    checks = []
    evidence = {}
    result = {
        "schema": "resume054-independent-terminal-verification.v1",
        "run_id": RUN, "restart_id": RESTART,
        "started_at": datetime.now(UTC).isoformat(),
        "acceptance_passed": False, "status": "pending",
        "checks": checks,
        "claims": {
            "new_pending_completion_boundary": "not_measured",
            "baseline_pair_pre_crash_bytes": "not_measured",
            "canonical_resume_completed_research": False,
            "gpu_llm_aos_training_security_acceptance": False,
            "independent_live_process_drain": "not_measured",
            "exact_outage_seconds": "not_measured",
        },
        "root_measured_outage_lower_bound": {
            "seconds": 33.818781544003286,
            "source_tool_chunk": "3f295c",
            "source_exit_code": 0,
            "measured_before_resume_launch_chunk": "13c496",
            "independently_remeasured_by_this_script": False,
        },
        "source_sha256": {name: sha((HERE / name).read_bytes())
                          for name in ("probe.py", "verify054.py")},
    }

    def check(name, condition, detail=None):
        checks.append({"name": name, "passed": bool(condition), "detail": detail})

    def group(name, function):
        try:
            function()
        except Exception as error:
            # Exception text can contain SQL parameters; only its class is exported.
            checks.append({"name": name, "passed": False,
                           "error_type": type(error).__name__})

    def query(role, sql):
        return normalized(probe.query(role, sql, {"run": RUN, "restart": RESTART}))

    try:
        state = load("state.json")
        assert state["run_id"] == RUN and state["restart_id"] == RESTART
        final = normalized(probe.snapshot(RUN))
        evidence["final_snapshot"] = final
        terminal = final["run"]["state"] in {"completed", "stopped", "failed"}
        check("terminal_run_with_report", terminal and bool(final["run"]["report_sha256"]))
        if not terminal:
            result["status"] = "pending_not_terminal"
            return 2
        before = load("before-crash.json")
        after = load("after-crash.json")["snapshot"]
        artifacts = ROOT / "data/runtime/director-artifacts" / RUN
        for name in ("state.json", "before-crash.json", "after-crash.json",
                     "dispatch-execution.json", "resume-execution.json"):
            result["source_sha256"][name] = sha((HERE / name).read_bytes())
        check("dispatch_intentional_nonzero", load("dispatch-execution.json")["exit_code"] != 0)
        check("resume_command_exit_zero", load("resume-execution.json")["exit_code"] == 0)

        def identity():
            check("original_request_preserved", all(
                final["run"][key] == before["run"][key] == after["run"][key]
                for key in ("request_json", "payload_sha256", "run_id")))
            check("original_execution_preserved", final["execution"] == before["execution"]
                  == after["execution"])
            execution = final["execution"][0]
            check("original3600second_deadline", (
                stamp(execution["deadline_at"]) - stamp(execution["started_at"])
            ).total_seconds() == 3600)
            owners = final["owners"]
            check("exact_generations1_then2", [row["generation"] for row in owners] == [1, 2])
            check("generation1_unchanged", owners[:1] == before["owners"] == after["owners"])
            gen2 = owners[1]
            check("generation2_binding", gen2["restart_id"] == RESTART
                  and gen2["execution_sha256"] == execution["execution_sha256"]
                  and gen2["worker_invocation_id"] != owners[0]["worker_invocation_id"]
                  and gen2["worker_unit"] == (
                      f"swapp-ai-scientist-director-resume-{UUID(RUN).hex}-{UUID(RESTART).hex}.service")
                  and final["control"][0]["current_generation"] == 2)
            restart = query("director", "SELECT * FROM lab.director_restart_requests "
                            "WHERE restart_id=:restart AND run_id=:run")[0]
            observation = query("director", "SELECT * FROM lab.director_restart_observations "
                                "WHERE restart_id=:restart")[0]
            evidence["restart"] = restart
            evidence["observation"] = observation
            proof = observation["observation_json"]
            check("restart_claim_and_original_window", restart["state"] == "claimed"
                  and restart["expected_generation"] == 1 and restart["claimant_generation"] == 2
                  and restart["execution_sha256"] == execution["execution_sha256"]
                  and 0 <= (stamp(gen2["claimed_at"]) - stamp(restart["created_at"])).total_seconds() < 120)
            check("drain_observation_canonical_hash", sha(canonical_bytes(proof))
                  == observation["observation_sha256"] == restart["observation_sha256"])
            check("drain_observation_identity", proof["run_id"] == RUN
                  and proof["restart_id"] == RESTART and proof["generation"] == 1
                  and proof["execution_sha256"] == execution["execution_sha256"]
                  and all(proof[key] is True for key in
                          ("owner_dead", "children_drained", "sandbox_drained"))
                  and all(proof["prior_owner"][key] == owners[0][key] for key in
                          ("worker_pid", "worker_start_ticks", "worker_boot_id", "worker_unit",
                           "worker_invocation_id", "worker_cgroup")))

        group("identity_group", identity)

        def checkpoints():
            rows = [item["event_json"] for item in final["checkpoints"]]
            assert len(rows) <= 10000
            mapped = {row["key"]: row for row in rows}
            check("unique_checkpoint_keys_and_sequences", len(mapped) == len(rows)
                  == len({row["sequence"] for row in rows}))
            payloads = {}
            total = 0
            for receipt in rows:
                raw = read_director_artifact(receipt["blob_sha256"], artifact_root=artifacts)
                total += len(raw)
                assert total <= 32 * 1024 * 1024
                assert sha(raw) == receipt["blob_sha256"] == receipt["payload_sha256"]
                payload = json.loads(raw)
                assert canonical_bytes(payload) == raw
                payloads[receipt["key"]] = payload
            check("all_checkpoint_artifacts_verified", True, {"count": len(rows), "bytes": total})
            check("entire_interruption_prefix_preserved", all(
                mapped[item["event_json"]["key"]] == item["event_json"]
                for snap in (before, after) for item in snap["checkpoints"]))
            proof_rows = evidence.get("observation", {}).get("observation_json", {}).get("checkpoints", [])
            check("drain_manifest_matches_final_and_interruption", bool(proof_rows)
                  and all(all(mapped[row["key"]][key] == row[key]
                              for key in ("sequence", "payload_sha256")) for row in proof_rows)
                  and {row["key"] for row in proof_rows}.issuperset(
                      item["event_json"]["key"] for item in after["checkpoints"]))
            key = f"proposal-budget-reconciled:{RUN}:1"
            pre_reconciled = any(item["event_json"]["key"] == key for item in after["checkpoints"])
            result["claims"]["new_pending_completion_boundary"] = (
                "not_exercised_already_reconciled" if pre_reconciled else "not_measured")
            proposal = payloads["proposal:1"]
            completion = proposal["completion_receipt"]
            reservation = payloads[f"proposal-budget-reservation:{RUN}:1"]
            check("original_completion_receipt_preserved", completion == before["verified_proposal_completion"])
            check("completion_bound_to_original_proposal", completion["schema"] == "director-proposal-completion.v1"
                  and completion["run_id"] == RUN and completion["ordinal"] == 1
                  and all(completion[k] == reservation[k] for k in
                          ("reservation_id", "wall_seconds", "model_tokens", "boot_id", "started_boottime"))
                  and all(completion[k] == proposal[k] for k in
                          ("candidate_sha256", "inputs_sha256", "deterministic_provider_id",
                           "provider_config_sha256", "provider_registry_entry_sha256"))
                  and math.isfinite(completion["measured_wall_seconds"])
                  and 0 <= completion["measured_wall_seconds"] <= completion["wall_seconds"]
                  and completion["completed_boottime"] - completion["started_boottime"]
                  == completion["measured_wall_seconds"])
            budgets = [payload["budget"] for payload in payloads.values()
                       if isinstance(payload.get("budget"), dict)]
            check("zero_model_and_bounded_proposal_counters", bool(budgets) and all(
                b["model_tokens"] == b["reserved_model_tokens"] == 0
                and 0 <= b["proposal_count"] <= 20 for b in budgets))
            evidence["budget_snapshots"] = budgets
            evidence["completion_receipt"] = completion
            # Different checkpoint schemas may omit elapsed; never invent it.
            elapsed = [b["elapsed_wall_seconds"] for b in budgets if "elapsed_wall_seconds" in b]
            check("recorded_elapsed_floor_nondecreasing", bool(elapsed)
                  and all(a <= b for a, b in zip(elapsed, elapsed[1:])))
            elapsed_at_claim = (stamp(final["owners"][1]["claimed_at"])
                                - stamp(final["execution"][0]["started_at"])).total_seconds()
            check("final_elapsed_includes_original_start_through_takeover", bool(elapsed)
                  and max(elapsed) >= elapsed_at_claim - 1.0,
                  {"elapsed_at_generation2_claim": elapsed_at_claim,
                   "recorded_elapsed_max": max(elapsed) if elapsed else None,
                   "clock_comparison_tolerance_seconds": 1.0})

        group("checkpoint_group", checkpoints)

        def baseline_pairs():
            baseline = lambda snap: [row for row in snap["scores"] if row["evaluation_kind"] == "baseline"]
            check("nine_baseline_rows_byte_equal", len(baseline(final)) == 9
                  and baseline(final) == baseline(before) == baseline(after))
            calibration = query("director", "SELECT lab.baseline_calibration_receipt(:run) AS receipt")[0]["receipt"]
            raw = read_director_artifact(calibration["blob_sha256"], artifact_root=artifacts)
            check("calibration_preserved_and_verified", calibration == before["calibration"]
                  and sha(raw) == calibration["blob_sha256"] == calibration["calibration_sha256"])
            count = query("director", "SELECT count(*) AS n FROM lab.experiments WHERE run_id=:run")[0]["n"]
            assert 1 <= count <= 64
            engine = probe.engine("director")
            try:
                pairs = read_run_pairs(engine, UUID(RUN), artifact_root=artifacts)
            finally:
                engine.dispose()
            check("every_final_pair_strict_hash_verified", len(pairs) == count)
            check("all20_grid_proposals_terminal", len([p for p in pairs if p.document.kind == "proposal"]) == 20)
            check("three_baseline_pairs", len([p for p in pairs if p.document.kind == "baseline"]) == 3)
            check("all_pair_model_tokens_zero", all(p.document.llm_input_tokens == p.document.llm_output_tokens == 0
                                                    for p in pairs))
            evidence["pairs"] = [{"experiment_id": p.document.experiment_id,
                                  "kind": p.document.kind, "status": p.document.status,
                                  "sequence": p.sequence} for p in pairs]

        group("baseline_pairs_group", baseline_pairs)

        def report_plan():
            tables = ("score_jobs", "task_scores", "task_terminal_outcomes", "run_tasks", "task_completions")
            data = {}
            for table in tables:
                # Names are this fixed source tuple, never external inputs.
                # Completion rows are internal trigger receipts; only the existing
                # migration role can read them. probe.engine forces read-only
                # transactions and a five-second statement limit for every role.
                role = "migrator" if table == "task_completions" else "scorer"
                data[table] = query(role, f"SELECT * FROM scorer.{table} WHERE run_id=:run LIMIT 4097")
                assert len(data[table]) <= 4096
            evidence["scorer"] = data
            jobs = data["score_jobs"]
            check("no_active_jobs", not final["jobs"] and all(j["state"] not in {"queued", "running"} for j in jobs))
            check("no_duplicate_score_cells_or_jobs", all(
                len({cell(row) for row in data[name]}) == len(data[name]) for name in ("score_jobs", "task_scores")))
            check("generation2_actual_scoring_progress", any(j["admitted_generation"] == 2
                  and any(str(s["score_job_id"]) == str(j["job_id"]) for s in data["task_scores"]) for j in jobs))
            check("jobs_bound_to_ancestry", all(j["admitted_generation"] in {1, 2}
                  and j["execution_sha256"] == final["execution"][0]["execution_sha256"] for j in jobs))
            planned = {cell(row) for row in data["run_tasks"]}
            scored = {cell(row) for row in data["task_scores"]}
            outcomes = {cell(row) for row in data["task_terminal_outcomes"]}
            completed = {cell(row) for row in data["task_completions"]}
            seal = query("director", "SELECT task_plan_count,task_plan_sha256 FROM lab.runs WHERE run_id=:run")[0]
            check("sealed_plan_exact_cell_coverage", bool(seal["task_plan_sha256"])
                  and seal["task_plan_count"] == len(planned) == len(data["run_tasks"])
                  and not scored.intersection(outcomes) and planned == scored | outcomes == completed)
            status, api = probe.api("GET", f"/v1/runs/{RUN}/report")
            stored = query("director", "SELECT report_json,report_sha256 FROM lab.reports WHERE run_id=:run")[0]
            raw = canonical_bytes(api["report"])
            assert len(raw) <= 4 * 1024 * 1024
            check("API_DB_canonical_report_equal", status == 200 and api["run_id"] == RUN
                  and api["report"] == stored["report_json"]
                  and sha(raw) == api["report_sha256"] == stored["report_sha256"] == final["run"]["report_sha256"])
            report = api["report"]
            check("report_identity_and_scope", report["run_id"] == RUN
                  and report["status"] == final["run"]["state"] and report["admitted_generation"] == 2
                  and report["execution_sha256"] == final["execution"][0]["execution_sha256"]
                  and report.get("benchmark_acceptance") is False and report.get("provider") == "mode-grid")
            expected = [{**{key: row[key] for key in CELL}, **row["score"]} for row in data["task_scores"]]
            check("report_scores_match_database", sorted(expected, key=cell) == sorted(report["task_scores"], key=cell))
            check("report_outcome_cells_match_database", {cell(r) for r in report["task_terminal_outcomes"]} == outcomes)
            expected_pairs = sorted({
                (row["admitted_generation"], row["execution_sha256"])
                for row in jobs + data["task_terminal_outcomes"]
            })
            check("report_receipt_ancestry_matches_database", report.get("receipt_execution_pairs") == [
                {"admitted_generation": generation, "execution_sha256": digest}
                for generation, digest in expected_pairs])
            (HERE / "canonical-report054.json").write_bytes(raw)
            evidence["report_sha256"] = sha(raw)
            evidence["report"] = report

        group("report_plan_group", report_plan)
        check("research_completed", final["run"]["state"] == "completed")
        result["acceptance_passed"] = all(item["passed"] for item in checks)
        result["claims"]["canonical_resume_completed_research"] = result["acceptance_passed"]
        result["status"] = "passed" if result["acceptance_passed"] else "failed"
        return 0 if result["acceptance_passed"] else 1
    except Exception as error:
        result["status"] = "failed"
        checks.append({"name": "verification_execution", "passed": False, "error_type": type(error).__name__})
        return 1
    finally:
        signal.alarm(0)
        result["finished_at"] = datetime.now(UTC).isoformat()
        result["evidence"] = evidence
        output = HERE / "verification054.json"
        encoded = json.dumps(result, indent=2, default=str, allow_nan=False) + "\n"
        oversized = len(encoded.encode()) > 32 * 1024 * 1024
        if oversized:
            result["evidence"] = {"omitted": "verification evidence exceeds32MiB"}
            result["acceptance_passed"] = False
            result["claims"]["canonical_resume_completed_research"] = False
            result["status"] = "failed"
            checks.append({"name": "bounded_evidence_size", "passed": False})
            encoded = json.dumps(result, indent=2, default=str, allow_nan=False) + "\n"
        output.write_text(encoded)
        print(json.dumps({"status": result["status"], "acceptance_passed": result["acceptance_passed"],
                          "checks": len(checks), "failed_checks": [r["name"] for r in checks if not r["passed"]],
                          "output": str(output), "sha256": sha(output.read_bytes())}))
        if oversized:
            return 1


if __name__ == "__main__":
    sys.exit(main())
