"""Keep score-job insertion inside the trusted planner boundary."""

from __future__ import annotations

from alembic import op

revision = "0006_narrow_director_score_jobs"
down_revision = "0005_planner_run_read"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Director can inspect queue status, but only Planner can enqueue/update."""
    op.execute("REVOKE INSERT, UPDATE ON scorer.score_jobs FROM swapp_lab_director")
    op.execute("GRANT SELECT ON scorer.score_jobs TO swapp_lab_director")


def downgrade() -> None:
    """Restore the prior Director queue privileges."""
    op.execute("REVOKE SELECT ON scorer.score_jobs FROM swapp_lab_director")
    op.execute("GRANT INSERT, SELECT, UPDATE ON scorer.score_jobs TO swapp_lab_director")
