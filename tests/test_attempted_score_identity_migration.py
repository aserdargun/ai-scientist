"""Schema regression for the actual attempted-stop scored-result query."""

import ast
import importlib.util
import re
from pathlib import Path

from lab.db.schema import score_jobs, task_scores

ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT / "lab/db/migrations/versions/0037_attempted_proposal_stop.py"
NEW = ROOT / "lab/db/migrations/versions/0039_attempted_score_identity.py"


def migration():
    spec = importlib.util.spec_from_file_location("attempted_score_identity", NEW)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def original_shape():
    source = OLD.read_text()
    start = source.index("CREATE FUNCTION lab.assert_attempted_proposal_shape(")
    return source[start:source.index("END $$;", start)]


def references(source, alias):
    return set(re.findall(r"\b" + alias + r"\.(\w+)", source))


def test_historical_failure_is_real_schema_mismatch_and_additive_fix_resolves_it():
    module = migration()
    original = original_shape()
    assert references(original, "s") - set(task_scores.c.keys()) == {
        "admitted_generation", "execution_sha256"
    }
    assert original.count(module.OLD_IDENTITY) == 1
    repaired = original.replace(module.OLD_IDENTITY, module.NEW_IDENTITY)
    assert references(repaired, "s") <= set(task_scores.c.keys())
    assert references(repaired, "j") <= set(score_jobs.c.keys())
    assert module.down_revision == "0038_attempted_stop_recovery"


def test_job_generation_and_score_provenance_guards_remain_exact():
    module = migration()
    repaired = original_shape().replace(module.OLD_IDENTITY, module.NEW_IDENTITY)
    for guard in (
        "j.execution_sha256 IS DISTINCT FROM p_execution",
        "j.admitted_generation IS DISTINCT FROM p_generation",
        "lab.valid_director_restart_ancestry(p_run,j.admitted_generation,p_generation,p_execution)",
        "s.score_job_id=j.job_id AND s.claim_token IS NULL",
        "(s.run_id,s.experiment_id,s.evaluation_kind,s.task_id,s.seed)",
        "s.worker_invocation_id ~ '^[0-9a-f]{32}$' AND c.completion_kind='scored'",
        "s.score->>'candidate_sha256'=j.candidate_sha256",
        "s.score->>'candidate_output_sha256'=j.artifact_sha256",
        "s.score->>'profile_sha256'=d.profile_sha256",
        "s.score->>'harness_sha256'=x.execution_json->>'harness_sha256'",
    ):
        assert guard in repaired
    assert repaired.replace(module.NEW_IDENTITY, module.OLD_IDENTITY) == original_shape()


def test_normal_score_producer_already_binds_invocation_and_job_generation():
    invocation = (ROOT / "lab/db/migrations/versions/0009_scorer_invocation_fence.py").read_text()
    generation = (ROOT / "lab/db/migrations/versions/0025_director_generations.py").read_text()
    assert "claim.claim_invocation_id IS DISTINCT FROM NEW.worker_invocation_id" in invocation
    assert "NEW.claim_token := NULL" in invocation
    assert "job.job_id=NEW.score_job_id AND job.run_id=run_target" in generation
    assert "job.admitted_generation=generation_target" in generation
    assert "job.execution_sha256=execution_target" in generation


def test_upgrade_only_replaces_one_expected_function_fragment_without_new_authority():
    module = ast.parse(NEW.read_text())
    upgrade = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
    sql = upgrade.body[1].value.args[0].value
    assert "<> 1" in sql and "source shape changed" in sql
    assert "EXECUTE replace(definition,old_identity,new_identity)" in sql
    assert "lab.assert_attempted_proposal_shape(uuid,text,integer,text)" in sql
    assert re.search(r"\b(GRANT|UPDATE|INSERT|DELETE|ALTER TABLE)\b", sql) is None
    assert "session_replication_role" not in sql
    assert "DISABLE TRIGGER" not in sql
