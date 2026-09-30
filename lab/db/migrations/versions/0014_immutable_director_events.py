"""Make run events append-only for runtime roles."""

from __future__ import annotations

from alembic import op

revision = "0014_immutable_events"
down_revision = "0013_baseline_calibration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Protect durable resume checkpoints from runtime mutation."""
    op.execute("REVOKE UPDATE, DELETE ON lab.run_events FROM swapp_lab_director")
    op.execute(
        """
        CREATE FUNCTION lab.reject_runtime_run_event_mutation()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        BEGIN
            IF session_user <> 'swapp_lab_migrator' THEN
                RAISE EXCEPTION 'run events are append-only';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER run_events_append_only
        BEFORE UPDATE OR DELETE ON lab.run_events
        FOR EACH ROW EXECUTE FUNCTION lab.reject_runtime_run_event_mutation()
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION lab.reject_runtime_run_event_mutation() FROM PUBLIC"
    )


def downgrade() -> None:
    """Restore the prior runtime grants and remove the append-only trigger."""
    op.execute("DROP TRIGGER run_events_append_only ON lab.run_events")
    op.execute("DROP FUNCTION lab.reject_runtime_run_event_mutation()")
    op.execute("GRANT UPDATE ON lab.run_events TO swapp_lab_director")

