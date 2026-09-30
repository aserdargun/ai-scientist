"""Give every planned evaluation and seed its own score identity."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_evaluation_identity"
down_revision = "0002_trusted_task_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Extend task plans and score keys without discarding any existing rows."""
    for table in ("run_tasks", "task_scores"):
        op.add_column(
            table,
            sa.Column("experiment_id", sa.String(128), nullable=False, server_default="legacy"),
            schema="scorer",
        )
        op.add_column(
            table,
            sa.Column("evaluation_kind", sa.String(24), nullable=False, server_default="primary"),
            schema="scorer",
        )
        op.add_column(
            table,
            sa.Column("seed", sa.Integer, nullable=False, server_default="0"),
            schema="scorer",
        )
    op.add_column(
        "run_tasks",
        sa.Column("candidate_sha256", sa.String(64), nullable=False, server_default="0" * 64),
        schema="scorer",
    )
    op.drop_constraint("run_tasks_pkey", "run_tasks", schema="scorer", type_="primary")
    op.create_primary_key(
        "run_tasks_pkey",
        "run_tasks",
        ["run_id", "experiment_id", "evaluation_kind", "task_id", "seed"],
        schema="scorer",
    )
    op.create_check_constraint(
        "ck_run_tasks_eval",
        "run_tasks",
        "evaluation_kind in ('baseline','primary','confirmation')",
        schema="scorer",
    )
    op.create_check_constraint(
        "ck_run_tasks_seed", "run_tasks", "seed >= 0", schema="scorer"
    )
    op.create_check_constraint(
        "ck_run_tasks_candidate_sha",
        "run_tasks",
        "length(candidate_sha256) = 64",
        schema="scorer",
    )
    op.drop_constraint("task_scores_pkey", "task_scores", schema="scorer", type_="primary")
    op.create_primary_key(
        "task_scores_pkey",
        "task_scores",
        ["run_id", "experiment_id", "evaluation_kind", "task_id", "seed"],
        schema="scorer",
    )
    op.create_foreign_key(
        "fk_task_scores_run_task",
        "task_scores",
        "run_tasks",
        ["run_id", "experiment_id", "evaluation_kind", "task_id", "seed"],
        ["run_id", "experiment_id", "evaluation_kind", "task_id", "seed"],
        source_schema="scorer",
        referent_schema="scorer",
        ondelete="CASCADE",
    )
    op.execute("GRANT SELECT ON scorer.run_tasks TO swapp_lab_scorer")


def downgrade() -> None:
    """Restore legacy task keys only when the richer identity is unused."""
    op.drop_constraint(
        "fk_task_scores_run_task", "task_scores", schema="scorer", type_="foreignkey"
    )
    op.drop_constraint("task_scores_pkey", "task_scores", schema="scorer", type_="primary")
    op.create_primary_key("task_scores_pkey", "task_scores", ["run_id", "task_id"], schema="scorer")
    op.drop_constraint("ck_run_tasks_candidate_sha", "run_tasks", schema="scorer", type_="check")
    op.drop_constraint("ck_run_tasks_seed", "run_tasks", schema="scorer", type_="check")
    op.drop_constraint("ck_run_tasks_eval", "run_tasks", schema="scorer", type_="check")
    op.drop_constraint("run_tasks_pkey", "run_tasks", schema="scorer", type_="primary")
    op.create_primary_key("run_tasks_pkey", "run_tasks", ["run_id", "task_id"], schema="scorer")
    op.drop_column("task_scores", "seed", schema="scorer")
    op.drop_column("task_scores", "evaluation_kind", schema="scorer")
    op.drop_column("task_scores", "experiment_id", schema="scorer")
    op.drop_column("run_tasks", "candidate_sha256", schema="scorer")
    op.drop_column("run_tasks", "seed", schema="scorer")
    op.drop_column("run_tasks", "evaluation_kind", schema="scorer")
    op.drop_column("run_tasks", "experiment_id", schema="scorer")
