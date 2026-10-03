"""Source contracts only; native PostgreSQL and physical retirement need owned proof."""

import ast
import re
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "lab/db/migrations/versions/0038_attempted_stop_recovery.py"
)


def sql():
    module = ast.parse(MIGRATION.read_text())
    upgrade = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
    return "\n".join(n.value.args[0].value for n in upgrade.body
                     if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call))


def definition(name):
    source = sql()
    start = source.index("FUNCTION lab." + name + "(")
    return source[start:source.index("END $$;", start)]


def test_no_grants_and_every_private_helper_is_revoked():
    source = sql()
    assert re.search(r"\bGRANT\b", source) is None
    assert "REVOKE ALL ON lab.attempted_stop_recovery_attempts" in source
    for name in re.findall(r"CREATE FUNCTION lab\.(\w+)\(", source):
        assert "lab." + name + "(" in source[source.index("REVOKE ALL ON FUNCTION"):]
    assert "session_replication_role" not in source
    assert "DISABLE TRIGGER" not in source
    assert "lab.initial_director_claim" not in source


def test_original_v37_and_v36_are_name_only_private_fallbacks():
    source = sql()
    assert "pg_get_functiondef(p.oid)" in source
    assert "replace(definition,'FUNCTION lab.'||pair[1]||'('" in source
    assert "['reconcile_stopped_score_job','reconcile_stop_job_v37']" in source
    assert "RETURN lab.reconcile_stop_job_v37(p_stop,p_job,p_expected,p_recovery)" in source
    assert "RETURN lab.close_stopped_missing_v37(p_stop,p_experiment)" in source
    assert "CREATE OR REPLACE FUNCTION lab.assert_stopped_proposal_shape" not in source
    assert "CREATE OR REPLACE FUNCTION lab.assert_stopped_job_transition" not in source
    assert "UPDATE lab.director_stop_job_drains" not in source
    assert "UPDATE scorer.task_terminal_outcomes" not in source


def test_original_full_plan_compared_before_first_mutation_and_replay_bound():
    begin = definition("begin_stopped_proposal_closure")
    assert "attempted-proposal-stop.v2" in begin
    assert "array_agg(key ORDER BY key)" in begin
    assert "original_primary_plan_text" in begin
    assert begin.index("original primary plan or expected owner changed before admission") < (
        begin.index("seconds:=lab.begin_proposal_stop_v37")
    )
    assert "jsonb_agg(to_jsonb(t) ORDER BY task_id,seed)" in begin
    assert "jsonb_array_length(plan::jsonb) NOT BETWEEN 1 AND 1024" in begin
    assert "p.original_primary_plan_text IS DISTINCT FROM plan" in begin
    assert "p.admitted_inventory_text IS DISTINCT FROM" in begin
    assert "sha256(convert_to(p_proposal,'UTF8'))" in begin
    assert "RETURN lab.assert_attempted_stop_window(a.run_id,p_stop)" in begin


def test_all_v2_contexts_lock_and_recheck_owner_control_original_window():
    context = definition("assert_stop_v2_context")
    assert "lab.lock_run_plan(a.run_id)" in context
    assert "current_generation=a.expected_generation FOR UPDATE" in context
    assert "g.execution_sha256 IS DISTINCT FROM a.execution_sha256" in context
    assert "IS DISTINCT FROM a.owner_json" in context
    assert "lab.assert_attempted_stop_window(a.run_id,p_stop)" in context
    assert "p.original_primary_plan_json IS DISTINCT FROM" in context
    assert "p.recovery_roster_json IS DISTINCT FROM lab.stop_v2_roster" in context


def test_real_worker_identity_requires_six_fields_canonical_scorer_slice():
    identity = definition("assert_stop_v2_worker_identity")
    assert "worker_pid','worker_start_ticks','worker_unit']::text[]" in identity
    assert "jsonb_typeof(p_identity->'worker_pid') IS DISTINCT FROM 'number'" in identity
    assert "swapp-ai-scientist-scorer-[0-9a-f]{32}" in identity
    assert "swapp-ai-scientist-scorer.slice/" in identity
    assert "worker_start_ticks')::bigint<=0" in identity


def test_ancestry_registration_requires_retired_predecessor_and_keeps_actual_ids():
    finish = definition("finish_stopped_proposal_children")
    assert "latest.worker_identity=identity THEN RETURN 'registered'" in finish
    assert "retired_ordinal=latest.attempt_ordinal" in finish
    assert "coalesce(latest.attempt_ordinal,0)+1" in finish
    assert "stop v2 predecessor not retired or invocation reused" in finish
    ancestry = definition("assert_stop_v2_ancestry")
    assert "latest.recovery_invocation_id IS DISTINCT FROM p_current" in ancestry
    assert "w.recovery_invocation_id=d.recovery_invocation" in ancestry
    assert "sha256(convert_to(r.observation_text,'UTF8'))" in ancestry
    assert "r.observation_text::jsonb->'worker_identity'=w.worker_identity" in ancestry
    assert "TG_OP<>'INSERT'" in definition("guard_stop_v2_audit")


def test_director_retirement_binds_registered_tuple_actual_observation_and_no_rewrite():
    begin = definition("begin_stopped_proposal_closure")
    assert "attempted-stop-retirement.v2" in begin
    assert "attempt.worker_identity IS DISTINCT FROM identity" in begin
    assert "attempted-stop-retirement-observation.v2" in begin
    assert "observation->'process_retired' IS DISTINCT FROM 'true'::jsonb" in begin
    assert "observation->'cgroup_empty' IS DISTINCT FROM 'true'::jsonb" in begin
    assert "properties->>'MainPID' IS DISTINCT FROM '0'" in begin
    assert "properties->>'InvocationID'=identity->>'worker_invocation_id'" in begin
    assert "IF NOT EXISTS(SELECT 1 FROM lab.attempted_stop_recovery_retirements" in begin
    assert "UPDATE lab.attempted_stop_recovery_retirements" not in sql()


def test_raw_identity_text_hash_protocol_is_explicit_and_completion_rows_not_exported():
    source = sql()
    assert "'identity_text',worker_identity::text" in source
    assert "sha256(convert_to(worker_identity::text,'UTF8'))" in source
    assert "'completions_count'" in source and "'completions_sha256'" in source
    assert "'completions'," not in source


def test_child_seal_precedes_missing_final_seal_and_does_not_drain_closure():
    finish = definition("finish_stopped_proposal_children")
    assert "UPDATE lab.director_stop_closures" not in finish
    assert "SET child_inventory_text=child" in finish
    assert "RETURN 'children_drained'" in finish
    close = definition("close_stopped_unattempted_tasks")
    assert close.index("PERFORM lab.assert_stop_v2_ancestry") < close.index(
        "INSERT INTO scorer.task_terminal_outcomes"
    )
    assert close.index("INSERT INTO scorer.task_completions") < close.index(
        "UPDATE lab.director_stop_closures SET state='drained'"
    )
    assert "recovery_invocation=latest.recovery_invocation_id" in close
    assert "SET missing_primary_inventory_text=missing" in close


def test_missing_only_real_original_unresolved_primary_cells_without_jobs_or_scores():
    missing = definition("assert_stop_v2_missing_transition")
    assert "p_new->>'evaluation_kind' IS DISTINCT FROM 'primary'" in missing
    assert "p_new->>'seed' IS DISTINCT FROM '0'" in missing
    assert "jsonb_array_elements(p.original_primary_plan_json)" in missing
    assert "EXISTS(SELECT 1 FROM scorer.score_jobs" in missing
    assert "EXISTS(SELECT 1 FROM scorer.task_scores" in missing
    assert "EXISTS(SELECT 1 FROM scorer.task_completions" in missing
    assert "p_new->>'score_job_id' IS NOT NULL" in missing
    assert "p_new->>'worker_invocation_id' IS NOT NULL" in missing
    assert "p_new->>'recovery_invocation_id' IS NOT NULL" in missing
    assert "infrastructure_unattempted" in missing


def test_full_n_cell_coverage_required_and_final_digest_binds_plan_and_roster():
    coverage = definition("assert_stop_v2_coverage")
    assert "IS DISTINCT FROM jsonb_array_length(p.original_primary_plan_json)" in coverage
    assert "o.producer_role='swapp_lab_planner'" in coverage
    assert "o.admitted_generation=a.expected_generation" in coverage
    assert "o.execution_sha256=a.execution_sha256" in coverage
    source = sql()
    assert "'schema','attempted-stop-terminal.v2'" in source
    assert "'original_primary_plan',p.original_primary_plan_json" in source
    assert "'missing_primary_inventory',lab.stop_v2_missing_text" in source
    assert "'recovery_roster_json',lab.stop_v2_roster" in source


def test_drained_retry_validates_without_new_worker_or_mutation():
    close = definition("close_stopped_unattempted_tasks")
    replay = close[close.index("IF a.state='drained'"):close.index("IF a.state<>'pending'")]
    assert "lab.assert_stop_v2_coverage" in replay
    assert "p.terminal_inventory_text IS DISTINCT FROM" in replay
    assert "RETURN true" in replay
    assert "INSERT" not in replay and "UPDATE" not in replay
    assert "final proof requires reuse not new worker" in definition(
        "finish_stopped_proposal_children"
    )


def test_only_existing_internal_missing_settings_and_audit_preserving_downgrade():
    source = sql()
    assert set(re.findall(r"set_config\('([^']+)'", source)) == {
        "lab.stop_request", "lab.stop_missing"
    }
    module = ast.parse(MIGRATION.read_text())
    downgrade = next(n for n in module.body if isinstance(n, ast.FunctionDef)
                     and n.name == "downgrade")
    assert any(isinstance(n, ast.Raise) for n in ast.walk(downgrade))
    assert "DROP TABLE" not in source
