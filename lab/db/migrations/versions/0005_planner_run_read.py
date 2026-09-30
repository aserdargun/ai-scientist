"""Permit the trusted planner to bind work only to active Lab runs."""

from __future__ import annotations

from alembic import op

revision = "0005_planner_run_read"
down_revision = "0004_durable_score_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Give Planner read-only run visibility without API or label access."""
    op.execute("GRANT USAGE ON SCHEMA lab TO swapp_lab_planner")
    op.execute("GRANT SELECT ON lab.runs TO swapp_lab_planner")


def downgrade() -> None:
    """Revoke only Planner's run visibility."""
    op.execute("REVOKE SELECT ON lab.runs FROM swapp_lab_planner")
    op.execute("REVOKE USAGE ON SCHEMA lab FROM swapp_lab_planner")
