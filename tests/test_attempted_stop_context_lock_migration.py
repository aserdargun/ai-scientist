"""Source regression for real Scorer access to the private stopped context guard."""

import ast
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "lab/db/migrations/versions/0040_attempted_stop_context_lock.py"
PREVIOUS = ROOT / "lab/db/migrations/versions/0038_attempted_stop_recovery.py"


def migration():
    spec = importlib.util.spec_from_file_location("attempted_stop_context_lock", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def context_source():
    source = PREVIOUS.read_text()
    start = source.index("CREATE FUNCTION lab.assert_stop_v2_context(")
    return source[start:source.index("END $$;", start)]


def test_historical_context_calls_lock_that_rejects_genuine_scorer_role():
    lifecycle = (ROOT / "lab/db/migrations/versions/0025_director_generations.py").read_text()
    start = lifecycle.index("CREATE OR REPLACE FUNCTION lab.lock_run_plan(")
    lock = lifecycle[start:lifecycle.index("$$", lifecycle.index("END;", start))]
    assert "session_user NOT IN ('swapp_lab_director','swapp_lab_planner')" in lock
    assert "swapp_lab_scorer" not in lock
    assert "run lifecycle lock requires Director or Planner identity" in lock
    assert context_source().count(migration().OLD_LOCK) == 1
    assert "PERFORM lab.assert_stop_v2_context(p_stop);" in PREVIOUS.read_text()


def test_replacement_uses_identical_original_transaction_lock_namespace():
    module = migration()
    lifecycle = (ROOT / "lab/db/migrations/versions/0025_director_generations.py").read_text()
    start = lifecycle.index("lock_key := ('x' || substr(encode(sha256(uuid_send(p_run_id))")
    end = lifecycle.index(";", start)
    key = lifecycle[start:end].split(":=", 1)[1].strip().replace("p_run_id", "a.run_id")
    assert re.sub(r"\s+", "", module.NEW_LOCK) == re.sub(
        r"\s+", "", "IF session_user = 'swapp_lab_scorer' THEN "
        "PERFORM pg_advisory_xact_lock(" + key + "); "
        "ELSE PERFORM lab.lock_run_plan(a.run_id); END IF;"
    )
    repaired = context_source().replace(module.OLD_LOCK, module.NEW_LOCK)
    assert repaired.count(module.OLD_LOCK) == 1  # Retained Director/Planner branch.
    assert repaired.replace(module.NEW_LOCK, module.OLD_LOCK) == context_source()
    assert module.down_revision == "0039_attempted_score_identity"


def test_all_original_context_owner_generation_deadline_and_inventory_guards_remain():
    module = migration()
    repaired = context_source().replace(module.OLD_LOCK, module.NEW_LOCK)
    for guard in (
        "IF a.run_id IS NULL",
        "state='stop_requested' AND stop_requested FOR UPDATE",
        "current_generation=a.expected_generation FOR UPDATE",
        "p.stop_protocol_version IS DISTINCT FROM 2",
        "g.execution_sha256 IS DISTINCT FROM a.execution_sha256",
        "IS DISTINCT FROM a.owner_json",
        "lab.assert_attempted_stop_window(a.run_id,p_stop)",
        "p.original_primary_plan_json IS DISTINCT FROM",
        "p.recovery_roster_json IS DISTINCT FROM lab.stop_v2_roster(p_stop)",
        "p.baseline_inventory_sha256 IS DISTINCT FROM",
    ):
        assert guard in repaired
    assert repaired.index("IF a.run_id IS NULL") < repaired.index("pg_advisory_xact_lock")


def test_upgrade_is_one_fail_closed_private_replacement_without_new_authority():
    tree = ast.parse(MIGRATION.read_text())
    upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
    sql = upgrade.body[1].value.args[0].value
    assert "lab.assert_stop_v2_context(uuid,jsonb)" in sql
    assert "<> 1" in sql and "source shape changed" in sql
    assert "EXECUTE replace(definition,old_lock,new_lock)" in sql
    assert re.search(r"\b(GRANT|UPDATE|INSERT|DELETE|ALTER TABLE)\b", sql) is None
    assert "set_config" not in sql
    assert "session_replication_role" not in sql
    assert "lab.assert_stop_v2_context(uuid,jsonb)" in PREVIOUS.read_text().split(
        "REVOKE ALL ON FUNCTION", 1
    )[1]


def test_copied_downstream_reconcile_route_is_repaired_without_changing_its_guards():
    module = migration()
    previous = (ROOT / "lab/db/migrations/versions/0037_attempted_proposal_stop.py").read_text()
    start = previous.index("CREATE OR REPLACE FUNCTION lab.reconcile_stopped_score_job(")
    fallback = previous[start:previous.index("END $$;", start)]
    assert fallback.count(module.OLD_LOCK) == 1
    repaired = fallback.replace(module.OLD_LOCK, module.NEW_LOCK)
    assert repaired.replace(module.NEW_LOCK, module.OLD_LOCK) == fallback
    assert "IF session_user<>'swapp_lab_scorer'" in repaired
    assert "current_generation=a.expected_generation FOR UPDATE" in repaired
    assert "lab.assert_attempted_stop_window(a.run_id,p_stop)" in repaired
    assert (
        "original->>'admitted_generation' IS DISTINCT FROM a.expected_generation::text"
    ) in repaired
    assert "original->>'execution_sha256' IS DISTINCT FROM a.execution_sha256" in repaired
    assert "RETURN lab.reconcile_unextended_stopped_score_job" in repaired
    assert "lab.reconcile_stop_job_v37(uuid,uuid,text,text)" in MIGRATION.read_text()
    assert (
        "RETURN lab.reconcile_stop_job_v37(p_stop,p_job,p_expected,p_recovery)"
    ) in PREVIOUS.read_text()


def test_only_scorer_uses_direct_lock_other_roles_keep_captured_owner_guard():
    module = migration()
    assert module.NEW_LOCK.startswith("IF session_user = 'swapp_lab_scorer' THEN ")
    assert module.NEW_LOCK.endswith("ELSE PERFORM lab.lock_run_plan(a.run_id); END IF;")
    assert "FOREACH signature IN ARRAY ARRAY[" in MIGRATION.read_text()
    assert "END LOOP" in MIGRATION.read_text()
