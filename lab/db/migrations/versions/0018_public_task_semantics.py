"""Store family-specific public task timing and masks behind the Scorer role."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018_public_task_semantics"
down_revision = "0016_director_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add an independently permissioned table for labels-adjacent task semantics."""
    op.add_column(
        "dataset_profiles",
        sa.Column("task_family", sa.String(16), server_default="EVT", nullable=False),
        schema="scorer",
    )
    op.create_check_constraint(
        "ck_dataset_profile_family",
        "dataset_profiles",
        "task_family in ('EVT','PDM','NRM')",
        schema="scorer",
    )
    op.create_table(
        "dataset_task_semantics",
        sa.Column("dataset_id", sa.String(128), primary_key=True),
        sa.Column("split_id", sa.String(128), primary_key=True),
        sa.Column("session_id", sa.String(256), primary_key=True),
        sa.Column("task_family", sa.String(16), nullable=False),
        sa.Column("sampling_s", sa.Integer, nullable=False),
        sa.Column(
            "evaluation_times_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "masked_samples_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "failure_windows_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("semantics_sha256", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id", "split_id", "session_id"],
            [
                "scorer.dataset_profiles.dataset_id",
                "scorer.dataset_profiles.split_id",
                "scorer.dataset_profiles.session_id",
            ],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("task_family in ('EVT','PDM','NRM')", name="ck_dataset_task_family"),
        sa.CheckConstraint("sampling_s > 0", name="ck_dataset_task_sampling"),
        sa.CheckConstraint(
            "semantics_sha256 ~ '^[0-9a-f]{64}$'", name="ck_dataset_task_semantics_sha"
        ),
        schema="scorer",
    )
    # Labels, masks and failure windows are Scorer data. Planner deliberately receives
    # no grant on this relation even though it can read label-free dataset profiles.
    op.execute("GRANT SELECT ON scorer.dataset_task_semantics TO swapp_lab_scorer")
    op.execute(
        """
        CREATE OR REPLACE VIEW lab.dev_task_results WITH (security_barrier=true) AS
        SELECT s.run_id, s.experiment_id, s.evaluation_kind, s.task_id, s.seed,
               s.score->>'candidate_sha256' AS candidate_sha256,
               s.score->>'dataset_id' AS dataset_id,
               s.score->>'split_id' AS split_id,
               s.score->>'session_id' AS session_id,
               s.score->>'vus_pr' AS vus_pr,
               s.score->>'vus_roc' AS vus_roc,
               s.score->>'sample_count' AS sample_count,
               s.score->>'profile_sha256' AS profile_sha256,
               s.score->>'harness_sha256' AS harness_sha256,
               s.score->>'candidate_output_sha256' AS candidate_output_sha256,
               COALESCE(s.score->>'task_family', p.task_family) AS task_family,
               COALESCE(s.score->>'task_score', s.score->>'vus_pr') AS task_score,
               s.score->>'fa_per_day' AS fa_per_day,
               s.score->>'duty_fraction' AS duty_fraction,
               s.score->>'sampling_s' AS sampling_s,
               s.score->>'position_bias' AS position_bias
          FROM scorer.task_scores s
          JOIN scorer.run_tasks t USING (run_id, experiment_id, evaluation_kind, task_id, seed)
          JOIN scorer.dataset_profiles p
            ON (p.dataset_id, p.split_id, p.session_id) =
               (t.dataset_id, t.split_id, t.session_id)
         WHERE p.visibility = 'dev'
           AND p.profile_sha256 = s.score->>'profile_sha256'
           AND p.task_family = COALESCE(s.score->>'task_family', 'EVT')
           AND t.candidate_sha256 = s.score->>'candidate_sha256'
           AND t.evaluation_kind IN ('baseline','primary','confirmation')
        """
    )


def downgrade() -> None:
    """Remove Scorer-only task semantics."""
    # PostgreSQL cannot remove columns from a view with CREATE OR REPLACE VIEW.
    # Drop and restore the exact prior projection while preserving its grant.
    op.execute(
        """
        DROP VIEW lab.dev_task_results
        """
    )
    op.execute(
        """
        CREATE VIEW lab.dev_task_results WITH (security_barrier=true) AS
        SELECT s.run_id, s.experiment_id, s.evaluation_kind, s.task_id, s.seed,
               s.score->>'candidate_sha256' AS candidate_sha256,
               s.score->>'dataset_id' AS dataset_id,
               s.score->>'split_id' AS split_id,
               s.score->>'session_id' AS session_id,
               s.score->>'vus_pr' AS vus_pr,
               s.score->>'vus_roc' AS vus_roc,
               s.score->>'sample_count' AS sample_count,
               s.score->>'profile_sha256' AS profile_sha256,
               s.score->>'harness_sha256' AS harness_sha256,
               s.score->>'candidate_output_sha256' AS candidate_output_sha256
          FROM scorer.task_scores s
          JOIN scorer.run_tasks t USING (run_id, experiment_id, evaluation_kind, task_id, seed)
          JOIN scorer.dataset_profiles p
            ON (p.dataset_id, p.split_id, p.session_id) =
               (t.dataset_id, t.split_id, t.session_id)
         WHERE p.visibility = 'dev'
           AND p.profile_sha256 = s.score->>'profile_sha256'
           AND t.candidate_sha256 = s.score->>'candidate_sha256'
           AND t.evaluation_kind IN ('baseline','primary','confirmation')
        """
    )
    op.execute("GRANT SELECT ON lab.dev_task_results TO swapp_lab_director")
    op.execute("REVOKE ALL ON scorer.dataset_task_semantics FROM swapp_lab_scorer")
    op.drop_table("dataset_task_semantics", schema="scorer")
    op.drop_constraint(
        "ck_dataset_profile_family", "dataset_profiles", schema="scorer", type_="check"
    )
    op.drop_column("dataset_profiles", "task_family", schema="scorer")
