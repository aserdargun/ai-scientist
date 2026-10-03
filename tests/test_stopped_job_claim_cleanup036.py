"""Keep the stopped claim cleanup migration restricted to its exact two deltas."""

import ast
import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "lab/db/migrations/versions"


def sql_literal(path, marker):
    return next(
        node.value
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and marker in node.value
    )


def definition(sql, name):
    start = sql.index("FUNCTION lab." + name + "(")
    return sql[start : sql.index("END $$;", start) + len("END $$;")]


def sql_tokens(value):
    # Preserve string literals exactly while allowing harmless SQL line wrapping.
    return re.findall(r"'(?:''|[^'])*'|[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[^\s]", value)


def test_migration_preserves_every_other_stopped_producer_and_guard_byte():
    original = sql_literal(VERSIONS / "0030_director_resume.py", "assert_stopped_job_transition(")
    updated = sql_literal(
        VERSIONS / "0036_stopped_job_claim_cleanup.py", "assert_stopped_job_transition("
    )
    # The two replacement strings span a few wrapped lines; normalize whitespace
    # outside quoted literals first, retaining every SQL token and literal.
    original_guard = definition(original, "assert_stopped_job_transition")
    original_producer = definition(original, "reconcile_stopped_score_job")
    updated_guard = definition(updated, "assert_stopped_job_transition")
    updated_producer = definition(updated, "reconcile_stopped_score_job")
    assert sql_tokens(updated_guard) == sql_tokens(
        original_guard.replace(
            "(p_new-ARRAY['state','error_code','updated_at'])=",
            "p_new->>'claimed_by' IS NULL AND p_new->>'lease_until' IS NULL AND "
            "(p_new-ARRAY['state','error_code','updated_at','claimed_by','lease_until'])=",
        ).replace(
            "(p_old-ARRAY['state','error_code','updated_at'])",
            "(p_old-ARRAY['state','error_code','updated_at','claimed_by','lease_until'])",
        )
    )
    assert sql_tokens(updated_producer) == sql_tokens(
        original_producer.replace(
            "error_code='scorer_error',updated_at=",
            "error_code='scorer_error',claimed_by=NULL,lease_until=NULL,updated_at=",
        )
    )
    assert "GRANT " not in updated and "REVOKE " not in updated


def test_existing_invocation_guard_still_requires_cleanup_and_retained_claim_identity():
    source = (VERSIONS / "0009_scorer_invocation_fence.py").read_text()
    assert "NEW.claimed_by IS NOT NULL OR NEW.lease_until IS NOT NULL" in source
    assert "NEW.claim_unit IS DISTINCT FROM OLD.claim_unit" in source
    assert "NEW.claim_invocation_id IS DISTINCT FROM OLD.claim_invocation_id" in source
    assert "completion IS DISTINCT FROM (CASE NEW.state" in source
