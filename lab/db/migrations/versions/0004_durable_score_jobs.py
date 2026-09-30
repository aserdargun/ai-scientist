"""Add durable queue records for an OS-isolated Scorer worker."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_durable_score_jobs"
down_revision = "0003_evaluation_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the bounded identity-only handoff from Director to Scorer."""
    op.create_table(
        "score_jobs",
        sa.Column("job_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("experiment_id", sa.String(128), nullable=False),
        sa.Column("evaluation_kind", sa.String(24), nullable=False),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("seed", sa.Integer, nullable=False),
        sa.Column("candidate_sha256", sa.String(64), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="0"),
        sa.Column("claimed_by", sa.String(128)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "experiment_id", "evaluation_kind", "task_id", "seed"],
            [
                "scorer.run_tasks.run_id",
                "scorer.run_tasks.experiment_id",
                "scorer.run_tasks.evaluation_kind",
                "scorer.run_tasks.task_id",
                "scorer.run_tasks.seed",
            ],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "run_id",
            "experiment_id",
            "evaluation_kind",
            "task_id",
            "seed",
            name="uq_score_jobs_task",
        ),
        sa.CheckConstraint(
            "state in ('queued','running','completed','failed')", name="ck_score_jobs_state"
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_score_jobs_attempt"),
        sa.CheckConstraint("length(candidate_sha256) = 64", name="ck_score_jobs_candidate_sha"),
        sa.CheckConstraint("length(artifact_sha256) = 64", name="ck_score_jobs_artifact_sha"),
        schema="scorer",
    )
    op.create_index(
        "ix_score_jobs_ready",
        "score_jobs",
        ["state", "lease_until", "created_at"],
        schema="scorer",
    )
    op.execute("GRANT USAGE ON SCHEMA scorer TO swapp_lab_director")
    op.execute("GRANT SELECT, INSERT, UPDATE ON scorer.score_jobs TO swapp_lab_director")
    op.execute("GRANT USAGE ON SCHEMA scorer TO swapp_lab_planner")
    op.execute("GRANT USAGE ON SCHEMA lab TO swapp_lab_planner")
    op.execute("GRANT SELECT ON lab.runs TO swapp_lab_planner")
    op.execute("GRANT SELECT ON scorer.dataset_profiles TO swapp_lab_planner")
    op.execute("GRANT SELECT, INSERT ON scorer.run_tasks TO swapp_lab_planner")
    op.execute("GRANT SELECT, INSERT ON scorer.score_jobs TO swapp_lab_planner")
    op.execute("GRANT SELECT, UPDATE ON scorer.score_jobs TO swapp_lab_scorer")


def downgrade() -> None:
    """Remove queue permissions and records."""
    op.drop_index("ix_score_jobs_ready", table_name="score_jobs", schema="scorer")
    op.drop_table("score_jobs", schema="scorer")
    op.execute("REVOKE USAGE ON SCHEMA scorer FROM swapp_lab_director")
    op.execute("REVOKE USAGE ON SCHEMA scorer FROM swapp_lab_planner")
