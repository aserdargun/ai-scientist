"""Bind AOS task/run/action identities to Lab run ownership."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_external_run_mapping"
down_revision = "0014_immutable_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add immutable, unique external action mapping for authenticated AOS starts."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM lab.runs WHERE origin = 'aos') THEN
                RAISE EXCEPTION 'existing AOS runs require an explicit mapping backfill';
            END IF;
        END;
        $$
        """
    )
    op.add_column("runs", sa.Column("external_task_id", sa.String(37)), schema="lab")
    op.add_column("runs", sa.Column("external_run_id", sa.String(36)), schema="lab")
    op.add_column("runs", sa.Column("external_action_id", sa.String(39)), schema="lab")
    op.create_check_constraint(
        "ck_runs_external_identity",
        "runs",
        "(external_task_id is null and external_run_id is null and external_action_id is null) "
        "or (origin = 'aos' and external_task_id is not null and external_run_id is not null "
        "and external_action_id is not null and length(external_task_id) = 37 "
        "and external_task_id like 'task-%' and length(external_run_id) = 36 "
        "and external_run_id like 'run-%' and length(external_action_id) = 39 "
        "and external_action_id like 'action-%')",
        schema="lab",
    )
    op.create_index(
        "uq_runs_external_action_owner",
        "runs",
        ["origin", "owner_id", "external_action_id"],
        unique=True,
        schema="lab",
        postgresql_where=sa.text("external_action_id IS NOT NULL"),
        sqlite_where=sa.text("external_action_id IS NOT NULL"),
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_external_run_mapping()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        BEGIN
            IF NEW.origin = 'aos' THEN
                IF NEW.external_task_id IS NULL OR NEW.external_run_id IS NULL
                   OR NEW.external_action_id IS NULL
                   OR NEW.external_task_id !~ '^task-[0-9a-f]{32}$'
                   OR NEW.external_run_id !~ '^run-[0-9a-f]{32}$'
                   OR NEW.external_action_id !~ '^action-[0-9a-f]{32}$'
                   OR NEW.request_json->>'external_task_id' IS DISTINCT FROM NEW.external_task_id
                   OR NEW.request_json->>'external_run_id' IS DISTINCT FROM NEW.external_run_id
                   OR NEW.request_json->>'external_action_id'
                      IS DISTINCT FROM NEW.external_action_id THEN
                    RAISE EXCEPTION 'AOS run mapping does not match immutable request';
                END IF;
            ELSIF NEW.external_task_id IS NOT NULL OR NEW.external_run_id IS NOT NULL
               OR NEW.external_action_id IS NOT NULL THEN
                RAISE EXCEPTION 'external run mapping is reserved for AOS';
            END IF;
            IF TG_OP = 'UPDATE' AND (
                OLD.external_task_id IS DISTINCT FROM NEW.external_task_id
                OR OLD.external_run_id IS DISTINCT FROM NEW.external_run_id
                OR OLD.external_action_id IS DISTINCT FROM NEW.external_action_id
            ) THEN
                RAISE EXCEPTION 'external run mapping is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER runs_external_mapping_guard BEFORE INSERT OR UPDATE ON lab.runs "
        "FOR EACH ROW EXECUTE FUNCTION lab.guard_external_run_mapping()"
    )
    op.execute("REVOKE ALL ON FUNCTION lab.guard_external_run_mapping() FROM PUBLIC")


def downgrade() -> None:
    """Remove the AOS mapping guard and columns."""
    op.execute("DROP TRIGGER runs_external_mapping_guard ON lab.runs")
    op.execute("DROP FUNCTION lab.guard_external_run_mapping()")
    op.drop_index("uq_runs_external_action_owner", table_name="runs", schema="lab")
    op.drop_constraint("ck_runs_external_identity", "runs", schema="lab", type_="check")
    op.drop_column("runs", "external_action_id", schema="lab")
    op.drop_column("runs", "external_run_id", schema="lab")
    op.drop_column("runs", "external_task_id", schema="lab")
