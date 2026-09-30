"""Create isolated run ledger and scorer-only label tables."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0001_initial_ledger"
down_revision = None
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    """Create core run ledger tables and enforce database-role separation."""
    op.create_table(
        "runs",
        sa.Column("run_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("request_json", _JSON, nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="queued"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("report_sha256", sa.String(64)),
        sa.Column("stop_requested", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.CheckConstraint(
            "state in ('queued','running','stop_requested','completed','failed','stopped')",
            name="ck_runs_state",
        ),
        sa.CheckConstraint("origin in ('local','aos')", name="ck_runs_origin"),
        schema="lab",
    )
    op.create_index(
        "uq_runs_idempotency_owner",
        "runs",
        ["origin", "owner_id", "idempotency_key"],
        unique=True,
        schema="lab",
    )
    op.create_table(
        "run_events",
        sa.Column("event_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id", UUID(as_uuid=True), sa.ForeignKey("lab.runs.run_id", ondelete="CASCADE")
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("event_json", _JSON, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema="lab",
    )
    op.create_table(
        "reports",
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("report_sha256", sa.String(64), nullable=False),
        sa.Column("report_json", _JSON, nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        schema="lab",
    )
    op.create_table(
        "dataset_profiles",
        sa.Column("dataset_id", sa.String(128), primary_key=True),
        sa.Column("split_id", sa.String(128), primary_key=True),
        sa.Column("session_id", sa.String(256), primary_key=True),
        sa.Column("sample_count", sa.Integer, nullable=False),
        sa.Column("sliding_window", sa.Integer, nullable=False),
        sa.Column("profile_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint("sample_count > 0", name="ck_dataset_profile_sample_count"),
        sa.CheckConstraint("sliding_window > 0", name="ck_dataset_profile_window"),
        schema="scorer",
    )
    op.create_table(
        "dataset_labels",
        sa.Column("dataset_id", sa.String(128), primary_key=True),
        sa.Column("split_id", sa.String(128), primary_key=True),
        sa.Column("session_id", sa.String(256), primary_key=True),
        sa.Column("sample_index", sa.Integer, primary_key=True),
        sa.Column("is_anomaly", sa.Boolean, nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id", "split_id", "session_id"],
            [
                "scorer.dataset_profiles.dataset_id",
                "scorer.dataset_profiles.split_id",
                "scorer.dataset_profiles.session_id",
            ],
            ondelete="CASCADE",
        ),
        schema="scorer",
    )
    op.create_table(
        "task_scores",
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("task_id", sa.String(128), primary_key=True),
        sa.Column("score", _JSON, nullable=False),
        sa.Column("guard_results", _JSON, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema="scorer",
    )
    op.execute("REVOKE ALL ON SCHEMA lab, scorer FROM PUBLIC")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA lab, scorer FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA lab TO swapp_lab_director, swapp_lab_scorer")
    op.execute("GRANT SELECT, INSERT, UPDATE ON lab.runs, lab.run_events TO swapp_lab_director")
    op.execute("GRANT SELECT ON lab.reports TO swapp_lab_director")
    op.execute("GRANT SELECT ON lab.runs TO swapp_lab_scorer")
    op.execute("GRANT INSERT, SELECT ON lab.reports TO swapp_lab_scorer")
    op.execute("GRANT USAGE ON SCHEMA scorer TO swapp_lab_scorer")
    op.execute("GRANT SELECT ON scorer.dataset_profiles, scorer.dataset_labels TO swapp_lab_scorer")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON scorer.task_scores TO swapp_lab_scorer")
    op.execute(
        "GRANT UPDATE (state, updated_at, report_sha256, stop_requested) "
        "ON lab.runs TO swapp_lab_scorer"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE swapp_lab_migrator IN SCHEMA lab "
        "REVOKE ALL ON TABLES FROM PUBLIC"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE swapp_lab_migrator IN SCHEMA scorer "
        "REVOKE ALL ON TABLES FROM PUBLIC"
    )


def downgrade() -> None:
    """Drop ledger objects while preserving database roles and schemas."""
    op.drop_table("task_scores", schema="scorer")
    op.drop_table("dataset_labels", schema="scorer")
    op.drop_table("dataset_profiles", schema="scorer")
    op.drop_table("reports", schema="lab")
    op.drop_table("run_events", schema="lab")
    op.drop_index("uq_runs_idempotency_owner", table_name="runs", schema="lab")
    op.drop_table("runs", schema="lab")
