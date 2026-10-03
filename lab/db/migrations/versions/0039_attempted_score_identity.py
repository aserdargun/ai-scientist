"""Read scored generation identity from its immutable admitted score job."""

from alembic import op

revision = "0039_attempted_score_identity"
down_revision = "0038_attempted_stop_recovery"
branch_labels = None
depends_on = None

OLD_IDENTITY = (
    "AND s.admitted_generation=j.admitted_generation AND s.execution_sha256=p_execution"
)
NEW_IDENTITY = (
    "AND j.admitted_generation IS NOT NULL AND j.execution_sha256=p_execution"
)


def upgrade() -> None:
    """Repair only the invalid score columns, retaining every existing provenance guard."""
    op.execute(
        r"""
DO $$
DECLARE definition text; old_identity text; new_identity text;
BEGIN
 old_identity := 'AND s.admitted_generation=j.admitted_generation'
 || ' AND s.execution_sha256=p_execution';
 new_identity := 'AND j.admitted_generation IS NOT NULL AND j.execution_sha256=p_execution';
 SELECT pg_get_functiondef(
 'lab.assert_attempted_proposal_shape(uuid,text,integer,text)'::regprocedure)
 INTO definition;
 IF (length(definition)-length(replace(definition,old_identity,''))) / length(old_identity) <> 1
 THEN RAISE EXCEPTION 'attempted score identity source shape changed'; END IF;
 -- task_scores is linked to the exact immutable job. Its normal insertion guards
 -- validate live claim/invocation and admitted generation/execution on that job.
 -- The original outer predicate independently validates current proposal generation
 -- or frozen baseline restart ancestry; all score/cell/profile receipts stay intact.
 EXECUTE replace(definition,old_identity,new_identity);
END $$;
        """
    )


def downgrade() -> None:
    """Never restore a function which cannot read the actual score schema."""
    raise RuntimeError("0039 downgrade would restore invalid scored identity columns")
