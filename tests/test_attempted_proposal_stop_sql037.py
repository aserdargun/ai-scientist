"""Source invariants only; no native SQL, admission or process proof."""

import ast
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIGRATION = HERE / "0037_attempted_proposal_stop.py"
if not MIGRATION.is_file():
    MIGRATION = HERE.parent / "lab/db/migrations/versions/0037_attempted_proposal_stop.py"


def statements():
    module = ast.parse(MIGRATION.read_text())
    upgrade = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
    return [n.value.args[0].value for n in upgrade.body
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]


def function(sql, name):
    start = sql.index("FUNCTION lab." + name + "(")
    return sql[start:sql.index("END $$;", start)]


def test_private_clones_preserve_original_zero_and_cas_implementations():
    sql = "\n".join(statements())
    assert "pg_get_functiondef(p.oid)" in sql
    assert "definition:=replace(definition,'FUNCTION lab.'||pair[1]||'('" in sql
    assert "['reconcile_stopped_score_job','reconcile_unextended_stopped_score_job']" in sql
    assert "['assert_stopped_proposal_document','assert_unattempted_proposal_document']" in sql
    assert "CREATE OR REPLACE FUNCTION lab.assert_stopped_proposal_shape" not in sql
    producer = function(sql, "reconcile_stopped_score_job")
    assert "lab.lock_run_plan(a.run_id)" in producer
    assert "current_generation=a.expected_generation FOR UPDATE" in producer
    assert (
        "original->>'admitted_generation' IS DISTINCT FROM "
        "a.expected_generation::text" in producer
    )
    assert "original->>'execution_sha256' IS DISTINCT FROM a.execution_sha256" in producer
    assert (
        "lab.reconcile_unextended_stopped_score_job(p_stop,p_job,p_expected,p_recovery)" in producer
    )


def test_existing_acl_retained_and_all_new_private_helpers_revoked():
    sql = "\n".join(statements())
    assert re.search(r"\bGRANT\b", sql) is None
    assert "FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer" in sql
    for name in re.findall(r"CREATE FUNCTION lab\.(\w+)\(", sql):
        assert "REVOKE ALL ON FUNCTION lab." + name + "(" in sql
    assert "set_config(" not in sql
    assert "session_replication_role" not in sql
    assert "DISABLE TRIGGER" not in sql


def test_before_mutation_owner_and_original_time_fences_are_mandatory():
    sql = "\n".join(statements())
    begin = function(sql, "begin_attempted_proposal_stop")
    mutation = begin.index("INSERT INTO lab.director_stop_closures")
    assert begin.index("expected owner changed") < mutation
    assert (
        begin.index("lab.assert_attempted_stop_window") < mutation
    )
    window = function(sql, "assert_attempted_stop_window")
    assert "min(created_at)" in window and "event_type='run.stop_requested'" in window
    assert "least(deadline,first_stop+interval '120 seconds'" in window
    assert "closure_created+interval '120 seconds'" in window
    assert "deadline<=clock_timestamp()" in window


def test_exact_bounded_raw_text_inventory_hashes_and_replay_comparison():
    sql = "\n".join(statements())
    begin = function(sql, "begin_attempted_proposal_stop")
    assert (
        "inventory::jsonb IS DISTINCT FROM "
        "(SELECT jsonb_agg(to_jsonb(j) ORDER BY j.job_id)" in begin
    )
    assert "octet_length(inventory)>2097152" in begin
    assert "existing.admitted_inventory_text IS DISTINCT FROM inventory" in begin
    assert "sha256(convert_to(inventory,'UTF8'))" in begin
    assert "sha256(convert_to(envelope_raw,'UTF8'))" in begin
    assert "p_proposal:=envelope->>'proposal_text'" in begin
    assert "sha256(convert_to(p_proposal,'UTF8'))" in begin
    finish = function(sql, "finish_attempted_proposal_children")
    assert "terminal_text:=lab.attempted_terminal_inventory_text(p_stop)" in finish
    assert "sha256(convert_to(terminal_text,'UTF8'))" in finish
    assert "p.terminal_inventory_text IS DISTINCT FROM terminal_text" in finish


def test_narrow_primary_and_ancestral_completed_baseline_shape():
    shape = function("\n".join(statements()), "assert_attempted_proposal_shape")
    assert "n NOT BETWEEN 1 AND 1024" in shape
    assert "status IN ('primary_running','abandoned')" in shape
    assert "(evaluation_kind<>'primary' OR seed<>0)" in shape
    assert "candidate_count NOT BETWEEN 1 AND n" in shape
    assert "j.admitted_generation IS DISTINCT FROM p_generation" in shape
    assert (
        "lab.valid_director_restart_ancestry(p_run,j.admitted_generation,"
        "p_generation,p_execution)" in shape
    )
    assert "c.completion_kind='scored'" in shape
    assert "d.visibility='dev'" in shape
    assert "s.worker_invocation_id ~ '^[0-9a-f]{32}$'" in shape
    assert "s.claim_token IS NULL" in shape


def test_terminal_proof_retains_exact_original_and_rejects_unrelated_drain():
    terminal = function("\n".join(statements()), "assert_attempted_terminal_inventory")
    assert "current_job IS DISTINCT FROM original" in terminal
    assert "current_job->>'claimed_by' IS NOT NULL" in terminal
    assert "current_job->>'lease_until' IS NOT NULL" in terminal
    assert "d.original_job=original" in terminal
    assert "o.recovery_invocation_id=d.recovery_invocation" in terminal
    assert (
        "o.worker_invocation_id IS NOT DISTINCT FROM original->>'claim_invocation_id'" in terminal
    )
    assert "c.completion_kind='terminal'" in terminal
    assert "attempted stop has unrelated drain" in terminal


def test_new_document_null_verdict_and_original_training_provenance_retained():
    document = function("\n".join(statements()), "assert_attempted_proposal_document")
    assert "'stopped_during_primary_evaluation'" in document
    assert "'admitted_inventory_sha256',p.admitted_inventory_sha256" in document
    assert "'terminal_inventory_sha256',p.terminal_inventory_sha256" in document
    for field in ("decision", "wall_seconds", "fit_seconds", "score_seconds", "suite_score"):
        assert "p_document->'" + field + "' IS DISTINCT FROM 'null'::jsonb" in document
    assert "p_document->'per_task' IS DISTINCT FROM '[]'::jsonb" in document
    assert "jsonb_each_text(p_document->'guards') WHERE value<>'not_run'" in document
    assert (
        "p_document->>'parent_tree' IS DISTINCT FROM "
        "p.proposal_json->>'parent_tree_sha256'" in document
    )
    assert "lab.assert_attempted_terminal_inventory" in document


def test_downgrade_never_discards_immutable_audit():
    source = MIGRATION.read_text()
    downgrade = next(n for n in ast.parse(source).body
                     if isinstance(n, ast.FunctionDef) and n.name == "downgrade")
    assert any(isinstance(n, ast.Raise) for n in ast.walk(downgrade))
    assert "DROP TABLE" not in source


def test_native_worker_failure_and_recovery_drain_are_distinct_strict_sources():
    sql = "\n".join(statements())
    worker = sql[sql.index("CREATE FUNCTION lab.valid_attempted_worker_terminal("):
                 sql.index("CREATE FUNCTION lab.assert_attempted_proposal_shape(")]
    assert "o.recovery_invocation_id IS NULL" in worker
    assert "o.claim_token ~ '^[A-Za-z0-9_.:-]{1,128}$'" in worker
    assert "o.worker_invocation_id=p_original->>'claim_invocation_id'" in worker
    assert "o.claim_token=p_original->>'claimed_by'" in worker
    assert "j.claim_unit IS NULL" in worker and "j.claim_invocation_id IS NULL" in worker
    terminal = function(sql, "assert_attempted_terminal_inventory")
    assert "lab.valid_attempted_worker_terminal(target_job,original" in terminal
    assert "worker terminal must not acquire a recovery drain" in terminal
    assert "o.claim_token IS NULL" in terminal
    assert "d.original_job=original" in terminal


def test_terminal_digest_exposes_no_raw_completion_rows_to_marker_readers():
    sql = "\n".join(statements())
    terminal_text = sql[sql.index("CREATE FUNCTION lab.attempted_terminal_inventory_text("):
                        sql.index("CREATE FUNCTION lab.begin_attempted_proposal_stop(")]
    assert "'completions_count'" in terminal_text
    assert "'completions_sha256'" in terminal_text
    assert "'completions'," not in terminal_text
