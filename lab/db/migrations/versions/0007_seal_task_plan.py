"""Seal the complete trusted run task set before report finalization."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_seal_task_plan"
down_revision = "0006_narrow_director_score_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add one-way plan closure and reject task rows after sealing."""
    op.add_column("runs", sa.Column("task_plan_sha256", sa.String(64)), schema="lab")
    op.add_column("runs", sa.Column("task_plan_count", sa.Integer), schema="lab")
    op.create_check_constraint(
        "ck_runs_task_plan_seal",
        "runs",
        "(task_plan_sha256 is null and task_plan_count is null) or "
        "(length(task_plan_sha256) = 64 and task_plan_count > 0)",
        schema="lab",
    )
    op.execute(
        """
        CREATE FUNCTION scorer.guard_task_plan_seal()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer
        AS $$
        BEGIN
            IF OLD.task_plan_sha256 IS NOT NULL AND
               (NEW.task_plan_sha256 IS DISTINCT FROM OLD.task_plan_sha256 OR
                NEW.task_plan_count IS DISTINCT FROM OLD.task_plan_count) THEN
                RAISE EXCEPTION 'sealed task plan is immutable';
            END IF;
            IF (NEW.task_plan_sha256 IS NULL) <> (NEW.task_plan_count IS NULL) OR
               (NEW.task_plan_sha256 IS NOT NULL AND
                (length(NEW.task_plan_sha256) <> 64 OR NEW.task_plan_count <= 0)) THEN
                RAISE EXCEPTION 'task plan seal must contain a digest and positive task count';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_runs_task_plan_immutable
        BEFORE UPDATE OF task_plan_sha256, task_plan_count ON lab.runs
        FOR EACH ROW EXECUTE FUNCTION scorer.guard_task_plan_seal()
        """
    )
    op.execute(
        """
        CREATE FUNCTION scorer.guard_run_task_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer
        AS $$
        DECLARE
            current_state text;
            current_plan text;
        BEGIN
            SELECT state, task_plan_sha256 INTO current_state, current_plan
              FROM lab.runs WHERE run_id = NEW.run_id FOR SHARE;
            IF NOT FOUND OR current_state <> 'running' OR current_plan IS NOT NULL THEN
                RAISE EXCEPTION 'run task plan is closed or run is not active';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_run_tasks_only_before_seal
        BEFORE INSERT ON scorer.run_tasks
        FOR EACH ROW EXECUTE FUNCTION scorer.guard_run_task_insert()
        """
    )
    op.execute("REVOKE ALL ON FUNCTION scorer.guard_task_plan_seal() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION scorer.guard_run_task_insert() FROM PUBLIC")
    op.execute("GRANT UPDATE (task_plan_sha256, task_plan_count) ON lab.runs TO swapp_lab_planner")


def downgrade() -> None:
    """Remove closure guards and only the Planner's closure columns."""
    op.execute("DROP TRIGGER trg_run_tasks_only_before_seal ON scorer.run_tasks")
    op.execute("DROP TRIGGER trg_runs_task_plan_immutable ON lab.runs")
    op.execute("DROP FUNCTION scorer.guard_run_task_insert()")
    op.execute("DROP FUNCTION scorer.guard_task_plan_seal()")
    op.execute(
        "REVOKE UPDATE (task_plan_sha256, task_plan_count) ON lab.runs FROM swapp_lab_planner"
    )
    op.drop_constraint("ck_runs_task_plan_seal", "runs", schema="lab", type_="check")
    op.drop_column("runs", "task_plan_count", schema="lab")
    op.drop_column("runs", "task_plan_sha256", schema="lab")
