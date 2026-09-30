"""Independent real PostgreSQL proposal/document probes using owned synthetic rows.

The documents are explicit test fixtures, not measured candidate or LLM results.
This checks database lifecycle and identity boundaries, not M0.9 acceptance.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from lab.db.task_plan import run_plan_lock_key
from review_experiment_role_surface import ROOT, connect


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode()).hexdigest()


def proposal(run_id: object, sequence: int = 0) -> dict:
    return {
        "experiment_id": f"exp_{uuid4().hex}", "run_id": run_id,
        "sequence": sequence, "experiment_number": sequence + 1, "kind": "proposal",
        "baseline_name": None, "parent_experiment_id": None,
        "candidate_sha256": hashlib.sha256(b"review-only source identity").hexdigest(),
        "candidate_blob_sha256": hashlib.sha256(b"review-only source identity").hexdigest(),
        "inputs_sha256": digest({"context": "review-only"}),
        "move_type": "hparam", "system": "S1",
        "hypothesis": "Synthetic identity-boundary fixture; no candidate was executed.",
        "predicted_delta": 0.0,
        "proposal_json": {"agent_version": "review-fixture.v1", "fixture": True},
    }


def register(connection: psycopg.Connection, value: dict) -> str:
    fields = (
        "experiment_id", "run_id", "sequence", "experiment_number", "kind", "baseline_name",
        "parent_experiment_id", "candidate_sha256", "candidate_blob_sha256", "inputs_sha256",
        "move_type", "system", "hypothesis", "predicted_delta", "proposal_json",
    )
    args = [Jsonb(value[name]) if name == "proposal_json" else value[name] for name in fields]
    return connection.execute(
        "SELECT (lab.register_experiment(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)).experiment_id",
        args,
    ).fetchone()[0]


def documents(value: dict, *, status: str = "rejected") -> tuple[dict, dict]:
    decision = {"verdict": "REJECT", "delta": None, "ci_low": None, "noise_sd": 0.0,
                "reason": "review_fixture_no_evaluation"}
    experiment = {
        "schema": "experiment.v1", "experiment_id": value["experiment_id"],
        "run_id": str(value["run_id"]), "ordinal": value["sequence"] + 1,
        "agent_version": "review-fixture.v1", "parent_experiment_id": None,
        "candidate_sha256": value["candidate_sha256"],
        "candidate_blob_sha256": value["candidate_blob_sha256"],
        "move_type": value["move_type"], "system": value["system"],
        "hypothesis": value["hypothesis"], "predicted_delta": value["predicted_delta"],
        "inputs_sha256": value["inputs_sha256"], "parent_tree": "d" * 40, "child_tree": None,
        "harness_sha256": "e" * 64, "image_sha256": "f" * 64,
        "suite_id": "review-fixture-no-evaluation", "suite_version": 1,
        "per_task": [], "suite_score": None, "guards": {"runtime": "not_run"},
        "decision": decision, "status": status, "fit_seconds": None, "score_seconds": None,
        "llm_input_tokens": 0, "llm_output_tokens": 0, "wall_seconds": 0.0,
    }
    trajectory = {
        "schema": "trajectory.v1", "trajectory_id": f"trj_{uuid4().hex}",
        "run_id": str(value["run_id"]), "experiment_id": value["experiment_id"],
        "agent_version": experiment["agent_version"], "model_id": "fixture-no-llm",
        "usage_profile": "noncommercial_research", "source_provenance": [],
        "quantization": "none", "adapter": "none", "system": value["system"],
        "thinking": False, "temperature": 0.0, "top_p": 1.0,
        "context_template": "review-only", "inputs_sha256": value["inputs_sha256"],
        "messages_blob_sha256": digest([]), "tool_calls": 0, "outcome": decision,
        "quality_tier": "bronze", "secrets_scrubbed": False, "people_scrubbed": False,
        "raw_values_scrubbed": False, "exclusions": ["synthetic boundary test; never export"],
    }
    return experiment, trajectory


def commit(connection: psycopg.Connection, exp: dict, traj: dict) -> dict:
    return connection.execute(
        "SELECT lab.commit_experiment_documents(%s,%s,%s,%s,%s,%s,%s,%s)",
        (exp["experiment_id"], Jsonb(exp), digest(exp), digest(exp), Jsonb(traj),
         digest(traj), digest(traj), traj["messages_blob_sha256"]),
    ).fetchone()[0]


def main() -> None:
    source_paths = [ROOT / "lab/db/migrations/versions/0011_experiment_ledger.py",
                    ROOT / "lab/db/schema.py", ROOT / "lab/db/task_plan.py"]
    evidence = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Real PostgreSQL Director functions, owned synthetic rows and fabricated no-evaluation documents. Proves only tested registration, lifecycle, row/document identity, atomicity and retry boundaries. No Docker, LLM, metric, holdout, or full Director run is claimed.",
        "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in source_paths},
        "checks": {},
    }
    run_ids = [uuid4(), uuid4()]
    with connect("migrator") as migrator, connect("director") as director:
        revision = migrator.execute("SELECT version_num FROM lab.alembic_version").fetchone()[0]
        if revision != "0011_experiment_ledger":
            raise SystemExit("0011 must be applied before this review; no fixture created")
        evidence["database_revision"] = revision

        def reject(label: str, action: object) -> None:
            try:
                with director.transaction():
                    action()
                    evidence["checks"][label] = False
                    raise psycopg.Rollback()
            except psycopg.Error as error:
                evidence["checks"][label] = error.sqlstate in {"P0001", "23514"}
                evidence.setdefault("rejection_sqlstates", {})[label] = error.sqlstate

        def waits_for_plan_lock(label: str, action: object) -> None:
            with migrator.transaction():
                migrator.execute("SELECT pg_advisory_xact_lock(%s)", (run_plan_lock_key(run_ids[0]),))
                try:
                    with director.transaction():
                        director.execute("SET LOCAL lock_timeout = '150ms'")
                        action()
                        evidence["checks"][label] = False
                        raise psycopg.Rollback()
                except psycopg.Error as error:
                    evidence["checks"][label] = error.sqlstate == "55P03"
                    evidence.setdefault("lock_sqlstates", {})[label] = error.sqlstate

        try:
            for run_id in run_ids:
                request = {"fixture": "experiment-ledger-review", "run_id": str(run_id)}
                migrator.execute(
                    "INSERT INTO lab.runs(run_id,origin,owner_id,idempotency_key,payload_sha256,request_json,state) "
                    "VALUES(%s,'local',%s,%s,%s,%s,'running')",
                    (run_id, f"review-{run_id}", f"review-{run_id}", digest(request), Jsonb(request)),
                )
            first = proposal(run_ids[0])
            other = proposal(run_ids[1])
            evidence["checks"]["register_proposal"] = register(director, first) == first["experiment_id"]
            evidence["checks"]["identical_registration_retry"] = register(director, first) == first["experiment_id"]
            waits_for_plan_lock("registration_respects_plan_lock", lambda: register(director, first))
            waits_for_plan_lock(
                "transition_respects_plan_lock",
                lambda: director.execute("SELECT lab.transition_experiment(%s,'primary_running')", (first["experiment_id"],)),
            )
            changed = {**first, "hypothesis": "changed after registration"}
            reject("changed_proposal_retry_rejected", lambda: register(director, changed))
            register(director, other)
            foreign_parent = {**proposal(run_ids[0], 2), "parent_experiment_id": other["experiment_id"]}
            reject("foreign_run_parent_rejected", lambda: register(director, foreign_parent))
            missing_baseline = {**proposal(run_ids[0], 2), "kind": "baseline", "experiment_number": None}
            reject("missing_baseline_name_rejected", lambda: register(director, missing_baseline))
            director.execute("SELECT lab.transition_experiment(%s,'primary_running')", (first["experiment_id"],))
            try:
                director.execute("SELECT lab.transition_experiment(%s,'primary_running')", (first["experiment_id"],))
                evidence["checks"]["same_transition_retry_is_idempotent"] = True
            except psycopg.Error as error:
                evidence["checks"]["same_transition_retry_is_idempotent"] = False
                evidence.setdefault("rejection_sqlstates", {})["same_transition_retry"] = error.sqlstate
            exp, traj = documents(first)
            waits_for_plan_lock("document_commit_respects_plan_lock", lambda: commit(director, exp, traj))
            reject("uncommitted_record_has_no_receipt", lambda: director.execute(
                "SELECT lab.experiment_record_receipt(%s)", (first["experiment_id"],),
            ))
            scored = {**exp, "status": "scored"}
            reject("no_task_scores_cannot_commit_scored", lambda: commit(director, scored, traj))
            for field, replacement in (
                ("hypothesis", "rewritten hypothesis"), ("predicted_delta", 0.125),
                ("candidate_blob_sha256", "c" * 64), ("inputs_sha256", "c" * 64),
                ("parent_experiment_id", other["experiment_id"]), ("move_type", "features"),
                ("system", "S2"), ("ordinal", 19),
            ):
                changed_exp = {**exp, field: replacement}
                reject(f"changed_experiment_{field}_rejected", lambda e=changed_exp: commit(director, e, traj))
            for field, replacement in (
                ("run_id", str(run_ids[1])), ("inputs_sha256", "c" * 64),
                ("system", "S2"), ("agent_version", "different-agent"),
                ("outcome", {**traj["outcome"], "reason": "different-outcome"}),
            ):
                changed_traj = {**deepcopy(traj), field: replacement}
                reject(f"changed_trajectory_{field}_rejected", lambda t=changed_traj: commit(director, exp, t))
            row = migrator.execute(
                "SELECT status,(SELECT count(*) FROM lab.experiment_records WHERE experiment_id=e.experiment_id),"
                "(SELECT count(*) FROM lab.trajectory_records WHERE experiment_id=e.experiment_id) "
                "FROM lab.experiments e WHERE experiment_id=%s", (first["experiment_id"],),
            ).fetchone()
            evidence["checks"]["failed_document_writes_are_atomic"] = row == ("primary_running", 0, 0)
            result = commit(director, exp, traj)
            evidence["checks"]["valid_zero_prediction_commit"] = result["status"] == "committed"
            receipt = director.execute(
                "SELECT lab.experiment_record_receipt(%s)", (first["experiment_id"],),
            ).fetchone()[0]
            evidence["checks"]["receipt_contains_only_exact_identity_and_digests"] = receipt == {
                "experiment_id": first["experiment_id"], "run_id": str(run_ids[0]),
                "status": exp["status"], "experiment_sha256": digest(exp),
                "experiment_blob_sha256": digest(exp), "trajectory_sha256": digest(traj),
                "trajectory_blob_sha256": digest(traj), "messages_blob_sha256": traj["messages_blob_sha256"],
            }
            evidence["checks"]["identical_document_retry"] = commit(director, exp, traj)["status"] == "already_committed"
            director.execute("UPDATE lab.runs SET state='stop_requested',stop_requested=true WHERE run_id=%s", (run_ids[0],))
            evidence["checks"]["committed_retry_after_stop"] = commit(director, exp, traj)["status"] == "already_committed"
            abandoned_exp, abandoned_traj = documents(other, status="abandoned")
            director.execute("UPDATE lab.runs SET state='stop_requested',stop_requested=true WHERE run_id=%s", (run_ids[1],))
            evidence["checks"]["stopped_proposal_commits_abandoned"] = commit(director, abandoned_exp, abandoned_traj)["status"] == "committed"
            director.execute("UPDATE lab.runs SET state='stopped' WHERE run_id=%s", (run_ids[1],))
            evidence["checks"]["committed_retry_after_terminal_run"] = commit(director, abandoned_exp, abandoned_traj)["status"] == "already_committed"
        except Exception as error:
            evidence["error"] = {"type": type(error).__name__, "sqlstate": getattr(error, "sqlstate", None)}
        finally:
            try:
                with migrator.transaction():
                    for run_id in run_ids:
                        migrator.execute("DELETE FROM lab.runs WHERE run_id=%s", (run_id,))
                evidence["checks"]["owned_fixture_cleanup"] = migrator.execute(
                    "SELECT count(*) FROM lab.runs WHERE run_id=ANY(%s)", (run_ids,),
                ).fetchone()[0] == 0
            except psycopg.Error as error:
                evidence["cleanup_error"] = {"sqlstate": error.sqlstate, "owned_run_ids": [str(x) for x in run_ids]}
    evidence["source_unchanged"] = all(
        hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == sha
        for name, sha in evidence["source_sha256"].items()
    )
    evidence["all_passed"] = (
        not evidence.get("error") and not evidence.get("cleanup_error")
        and len(evidence["checks"]) >= 32 and all(evidence["checks"].values())
        and evidence["source_unchanged"]
    )
    evidence["command"] = ".venv/bin/python docs/ai-scientist/review-evidence/review_experiment_ledger.py"
    evidence["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name("experiment-ledger-review.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    if not evidence["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
