"""Persist Director ownership and idempotent interrupted-run recovery receipts."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016_director_recovery"
down_revision = "0015_external_run_mapping"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Bind every new Director claim to one immutable process generation."""
    op.create_table(
        "director_run_owners",
        sa.Column(
            "run_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("worker_pid", sa.Integer, nullable=False),
        sa.Column("worker_start_ticks", sa.BigInteger, nullable=False),
        sa.Column("worker_boot_id", sa.String(36), nullable=False),
        sa.Column("worker_unit", sa.String(255), nullable=False),
        sa.Column("worker_invocation_id", sa.String(32), nullable=False),
        sa.Column("worker_cgroup", sa.Text, nullable=False),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("length(payload_sha256) = 64", name="ck_director_owner_payload_sha"),
        sa.CheckConstraint("worker_pid > 1", name="ck_director_owner_pid"),
        sa.CheckConstraint("worker_start_ticks > 0", name="ck_director_owner_start_ticks"),
        sa.CheckConstraint(
            "worker_boot_id ~ '^[0-9a-f-]{36}$'",
            name="ck_director_owner_boot_id",
        ),
        sa.CheckConstraint(
            "worker_invocation_id ~ '^[0-9a-f]{32}$'",
            name="ck_director_owner_invocation",
        ),
        sa.CheckConstraint(
            "worker_unit ~ '^[A-Za-z0-9_.@-]+[.]service$'",
            name="ck_director_owner_unit",
        ),
        sa.CheckConstraint(
            "worker_cgroup ~ '^/.+[.]service$'",
            name="ck_director_owner_cgroup",
        ),
        schema="lab",
    )
    op.create_table(
        "director_recoveries",
        sa.Column("recovery_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("observed_run_state", sa.String(24), nullable=False),
        sa.Column("owner_pid", sa.Integer),
        sa.Column("owner_start_ticks", sa.BigInteger),
        sa.Column("owner_boot_id", sa.String(36)),
        sa.Column("owner_unit", sa.String(255)),
        sa.Column("owner_invocation_id", sa.String(32)),
        sa.Column("owner_cgroup", sa.Text),
        sa.Column("state", sa.String(16), nullable=False, server_default="started"),
        sa.Column("result_json", sa.JSON().with_variant(JSONB(), "postgresql")),
        sa.Column("result_sha256", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("length(request_sha256) = 64", name="ck_director_recovery_request_sha"),
        sa.CheckConstraint(
            "action = 'stop_and_finalize'",
            name="ck_director_recovery_action",
        ),
        sa.CheckConstraint(
            "observed_run_state in ('running','stop_requested','completed','failed','stopped')",
            name="ck_director_recovery_observed_state",
        ),
        sa.CheckConstraint(
            "state in ('started','pending','completed','failed')",
            name="ck_director_recovery_state",
        ),
        sa.CheckConstraint(
            "(owner_pid is null and owner_start_ticks is null and owner_boot_id is null "
            "and owner_unit is null and owner_invocation_id is null and owner_cgroup is null) "
            "or (owner_pid is not null and owner_start_ticks is not null "
            "and owner_boot_id is not null and owner_unit is not null "
            "and owner_invocation_id is not null and owner_cgroup is not null "
            "and owner_pid > 1 and owner_start_ticks > 0 and length(owner_boot_id) = 36 "
            "and length(owner_unit) > 0 and length(owner_invocation_id) = 32 "
            "and length(owner_cgroup) > 0)",
            name="ck_director_recovery_owner_identity",
        ),
        sa.CheckConstraint(
            "(result_json is null and result_sha256 is null) or "
            "(result_json is not null and result_sha256 is not null "
            "and length(result_sha256) = 64)",
            name="ck_director_recovery_result_digest",
        ),
        sa.UniqueConstraint("run_id", "recovery_id", name="uq_director_recovery_run_id"),
        schema="lab",
    )

    op.execute(
        """
        CREATE FUNCTION lab.guard_director_run_owner()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE current_run lab.runs%ROWTYPE;
        BEGIN
            IF session_user NOT IN ('swapp_lab_director','swapp_lab_migrator') THEN
                RAISE EXCEPTION 'Director owner receipt requires the Director role';
            END IF;
            IF TG_OP = 'DELETE' AND session_user = 'swapp_lab_migrator'
               AND pg_trigger_depth() > 1 THEN
                RETURN OLD;
            END IF;
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'Director owner receipts are immutable';
            END IF;
            SELECT * INTO current_run FROM lab.runs WHERE run_id=NEW.run_id FOR UPDATE;
            IF NOT FOUND OR current_run.state <> 'running'
               OR current_run.payload_sha256 IS DISTINCT FROM NEW.payload_sha256 THEN
                RAISE EXCEPTION 'Director owner receipt does not match a running run';
            END IF;
            IF NEW.worker_cgroup NOT LIKE '%/' || NEW.worker_unit THEN
                RAISE EXCEPTION 'Director owner unit and cgroup differ';
            END IF;
            IF NEW.worker_unit LIKE 'swapp-ai-scientist-director-dispatch-%'
               AND NEW.worker_unit <> 'swapp-ai-scientist-director-dispatch-' ||
                   replace(NEW.run_id::text,'-','') || '.service' THEN
                RAISE EXCEPTION 'transient Director unit is not bound to this run';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER director_run_owner_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON lab.director_run_owners
        FOR EACH ROW EXECUTE FUNCTION lab.guard_director_run_owner()
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_director_recovery()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        BEGIN
            IF session_user NOT IN ('swapp_lab_director','swapp_lab_migrator') THEN
                RAISE EXCEPTION 'run recovery requires the Director role';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.state <> 'started' THEN
                    RAISE EXCEPTION 'recovery intent must start in started state';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                IF session_user = 'swapp_lab_migrator' AND pg_trigger_depth() > 1 THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'recovery receipts are immutable';
            END IF;
            IF (OLD.recovery_id, OLD.run_id, OLD.request_sha256, OLD.action,
                OLD.observed_run_state, OLD.owner_pid, OLD.owner_start_ticks,
                OLD.owner_boot_id, OLD.owner_unit, OLD.owner_invocation_id,
                OLD.owner_cgroup, OLD.created_at)
               IS DISTINCT FROM
               (NEW.recovery_id, NEW.run_id, NEW.request_sha256, NEW.action,
                NEW.observed_run_state, NEW.owner_pid, NEW.owner_start_ticks,
                NEW.owner_boot_id, NEW.owner_unit, NEW.owner_invocation_id,
                NEW.owner_cgroup, NEW.created_at) THEN
                RAISE EXCEPTION 'recovery request identity is immutable';
            END IF;
            IF OLD.state IN ('completed','failed') AND
               (OLD.state,OLD.result_json,OLD.result_sha256)
               IS DISTINCT FROM (NEW.state,NEW.result_json,NEW.result_sha256) THEN
                RAISE EXCEPTION 'terminal recovery receipt is immutable';
            END IF;
            IF NEW.state NOT IN ('pending','completed','failed') OR
               OLD.state NOT IN ('started','pending') THEN
                RAISE EXCEPTION 'invalid recovery state transition';
            END IF;
            IF NEW.state='pending' AND
               (NEW.result_json IS NULL OR NEW.result_sha256 IS NULL) THEN
                RAISE EXCEPTION 'pending recovery requires a durable result';
            END IF;
            IF NEW.state IN ('completed','failed') AND
               (NEW.result_json IS NULL OR NEW.result_sha256 IS NULL) THEN
                RAISE EXCEPTION 'terminal recovery requires a durable result';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER director_recovery_guard
        BEFORE INSERT OR UPDATE OR DELETE ON lab.director_recoveries
        FOR EACH ROW EXECUTE FUNCTION lab.guard_director_recovery()
        """
    )
    op.execute("REVOKE ALL ON FUNCTION lab.guard_director_run_owner() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION lab.guard_director_recovery() FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT ON lab.director_run_owners TO swapp_lab_director")
    op.execute("GRANT SELECT, INSERT ON lab.director_recoveries TO swapp_lab_director")
    op.execute(
        "GRANT UPDATE (state,result_json,result_sha256,updated_at) "
        "ON lab.director_recoveries TO swapp_lab_director"
    )


def downgrade() -> None:
    """Remove Director recovery receipts."""
    op.execute("DROP TRIGGER director_recovery_guard ON lab.director_recoveries")
    op.execute("DROP TRIGGER director_run_owner_immutable ON lab.director_run_owners")
    op.execute("DROP FUNCTION lab.guard_director_recovery()")
    op.execute("DROP FUNCTION lab.guard_director_run_owner()")
    op.drop_table("director_recoveries", schema="lab")
    op.drop_table("director_run_owners", schema="lab")
