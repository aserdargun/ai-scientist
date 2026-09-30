"""Bind scorer tasks to trusted split profiles rather than candidate metadata."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0002_trusted_task_profiles"
down_revision = "0001_initial_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add immutable-by-runtime dataset profiles and run-to-task assignments."""
    op.create_table(
        "run_tasks",
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("task_id", sa.String(128), primary_key=True),
        sa.Column("dataset_id", sa.String(128), nullable=False),
        sa.Column("split_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(256), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id", "split_id", "session_id"],
            [
                "scorer.dataset_profiles.dataset_id",
                "scorer.dataset_profiles.split_id",
                "scorer.dataset_profiles.session_id",
            ],
            name="fk_run_tasks_dataset_profile",
            ondelete="RESTRICT",
        ),
        schema="scorer",
    )
    op.execute("GRANT SELECT ON scorer.run_tasks TO swapp_lab_scorer")


def downgrade() -> None:
    """Drop trusted task assignments."""
    op.drop_table("run_tasks", schema="scorer")
