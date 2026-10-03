"""Acquire the existing lifecycle transaction lock inside private stop context checks."""

from alembic import op

revision = "0040_attempted_stop_context_lock"
down_revision = "0039_attempted_score_identity"
branch_labels = None
depends_on = None

OLD_LOCK = "PERFORM lab.lock_run_plan(a.run_id);"
NEW_LOCK = (
    "IF session_user = 'swapp_lab_scorer' THEN PERFORM pg_advisory_xact_lock("
    "('x' || substr(encode(sha256(uuid_send(a.run_id)), 'hex'), 1, 16))::bit(64)::bigint); "
    "ELSE PERFORM lab.lock_run_plan(a.run_id); END IF;"
)


def upgrade() -> None:
    """Keep the same run lock and all guards without expanding the public lock RPC."""
    op.execute(
        r"""
DO $$
DECLARE definition text; old_lock text; new_lock text; signature text;
BEGIN
 old_lock := 'PERFORM lab.lock_run_plan(a.run_id);';
 new_lock := 'IF session_user = ''swapp_lab_scorer'' THEN PERFORM pg_advisory_xact_lock('
 || '(''x'' || substr(encode(sha256(uuid_send(a.run_id)), ''hex''), 1, 16))::bit(64)::bigint); '
 || 'ELSE PERFORM lab.lock_run_plan(a.run_id); END IF;';
 FOREACH signature IN ARRAY ARRAY[
 'lab.assert_stop_v2_context(uuid,jsonb)',
 'lab.reconcile_stop_job_v37(uuid,uuid,text,text)'
 ] LOOP
 SELECT pg_get_functiondef(signature::regprocedure) INTO definition;
 IF (length(definition)-length(replace(definition,old_lock,''))) / length(old_lock) <> 1
 THEN RAISE EXCEPTION 'attempted stop context lock source shape changed'; END IF;
 -- The private SECURITY DEFINER guards admit only a bound stopped
 -- run and rechecks owner/control generation, execution, original deadline and plan.
 -- This is the same SHA256 UUID namespace and transaction lock used by lock_run_plan;
 -- Director/Planner keep lock_run_plan and its captured owner_run validation;
 -- the public role guard and all function ACLs remain unchanged.
 EXECUTE replace(definition,old_lock,new_lock);
 END LOOP;
END $$;
        """
    )


def downgrade() -> None:
    """Do not restore the role-incompatible private context call."""
    raise RuntimeError("0040 downgrade would block genuine Scorer stopped-context checks")
