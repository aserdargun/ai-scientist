"""Real-role SQL finalization of synthetic calibrated mode runs stopped before a candidate.

Only a prefix-guarded disposable PostgreSQL database is used. No model, service,
GPU, process-death or scientific-measurement acceptance is represented.
"""

import json

import pytest
from sqlalchemy import text
from test_postgres_holdout030_recovery import _engines
from test_postgres_resume046_chain import _calibrated_stop_proposal055

from lab.director.ownership import reset_execution_owner
from lab.director.task_plan import seal_run_task_plan
from lab.scorer.service import IndependentScorer


@pytest.mark.live
def test_calibrated_mode_pre_candidate_stop_publishes_original_scorer_report(tmp_path, monkeypatch):
    monkeypatch.setattr("lab.scorer.jobs.MIN_FREE_DISK_BYTES", 0)
    engines = migrator, director, planner, scorer = _engines()
    fixture = None
    try:
        fixture = _calibrated_stop_proposal055(migrator, director, planner, scorer, tmp_path)
        run = fixture["run_id"]
        # Match the admitted operating-mode research profile; values are synthetic pins.
        with migrator.begin() as connection:
            original = connection.execute(
                text("SELECT request_json FROM lab.runs WHERE run_id=:run"), {"run": run}
            ).scalar_one()
            mode = original | dict(
                provider="local-qwen",
                track="mode",
                purpose="research",
                proposal_contract="operating-mode-config.v1",
                study_kind="single_snapshot_study",
                snapshot_sha256="a" * 64,
                provider_config_sha256="b" * 64,
                provider_registry_entry_sha256="c" * 64,
                suite_manifest_sha256="d" * 64,
            )
            connection.execute(
                text("UPDATE lab.runs SET request_json=CAST(:request AS jsonb) WHERE run_id=:run"),
                {"run": run, "request": json.dumps(mode)},
            )
        with director.begin() as connection:
            connection.execute(
                text("SELECT lab.request_director_run_stop(:run,:owner,'local')"),
                {"run": run, "owner": "fixture:runend-unavailable"},
            )
        seal_run_task_plan(planner, run_id=run)
        service = IndependentScorer(
            scorer, harness_sha256=fixture["execution"]["harness_sha256"], artifact_root=tmp_path
        )
        result = service.finalize_if_ready(
            run_id=run, admitted_generation=1, execution_sha256=fixture["owner"].execution_sha256
        )
        assert result is not None
        report, report_sha = result
        assert report["status"] == "stopped"
        assert report["proposal_contract"] == "operating-mode-config.v1"
        assert len(report["task_scores"]) == 9
        assert {row["evaluation_kind"] for row in report["task_scores"]} == {"baseline"}
        assert (
            service.complete_run(
                run_id=run,
                admitted_generation=1,
                execution_sha256=fixture["owner"].execution_sha256,
            )
            == result
        )
        with pytest.raises(ValueError, match="finalizer pair differs"):
            service.complete_run(
                run_id=run,
                admitted_generation=2,
                execution_sha256=fixture["owner"].execution_sha256,
            )
        with director.connect() as connection:
            state, stored_sha = connection.execute(
                text("SELECT state,report_sha256 FROM lab.runs WHERE run_id=:run"), {"run": run}
            ).one()
        assert state == "stopped" and stored_sha == report_sha
    finally:
        if fixture is not None:
            reset_execution_owner(fixture["owner_context_token"])
        for engine in engines:
            engine.dispose()
