"""Fence run-end admission when no safe query can be started or resumed."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_run_end_unavailable"
down_revision = "0020_holdout_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Install a bitless, immutable no-admission fence and its admission guards."""
    op.create_table(
        "holdout_run_end_unavailable",
        sa.Column("run_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("candidate_experiment_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("prior_state_key", sa.String(160), nullable=False),
        sa.Column("prior_state_sha256", sa.String(64), nullable=False),
        sa.Column("prior_state_sequence", sa.Integer, nullable=False),
        sa.Column("budget_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("unavailable_checkpoint_sha256", sa.String(64), nullable=False),
        sa.Column("unavailable_checkpoint_sequence", sa.Integer, nullable=False),
        sa.Column("original_intent_checkpoint_sha256", sa.String(64)),
        sa.Column("original_intent_checkpoint_sequence", sa.Integer),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["run_id"], ["lab.runs.run_id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "reason in ('fresh_wall_budget_unavailable', "
            "'intent_checkpoint_without_sql_registration')",
            name="ck_run_end_unavailable_reason",
        ),
        sa.CheckConstraint(
            "length(prior_state_sha256)=64 and prior_state_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_run_end_unavailable_state_sha",
        ),
        sa.CheckConstraint(
            "length(budget_snapshot_sha256)=64 and budget_snapshot_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_run_end_unavailable_budget_sha",
        ),
        sa.CheckConstraint(
            "length(unavailable_checkpoint_sha256)=64 and "
            "unavailable_checkpoint_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_run_end_unavailable_checkpoint_sha",
        ),
        sa.CheckConstraint(
            "(original_intent_checkpoint_sha256 is null and "
            "original_intent_checkpoint_sequence is null) or "
            "(original_intent_checkpoint_sha256 is not null and "
            "original_intent_checkpoint_sha256 ~ '^[0-9a-f]{64}$' and "
            "original_intent_checkpoint_sequence is not null and "
            "original_intent_checkpoint_sequence >= 0)",
            name="ck_run_end_unavailable_original_intent",
        ),
        sa.CheckConstraint(
            "(reason='fresh_wall_budget_unavailable' and "
            "original_intent_checkpoint_sha256 is null) or "
            "(reason='intent_checkpoint_without_sql_registration' and "
            "original_intent_checkpoint_sha256 is not null)",
            name="ck_run_end_unavailable_reason_intent",
        ),
        sa.CheckConstraint(
            "prior_state_sequence >= 0 and unavailable_checkpoint_sequence > prior_state_sequence",
            name="ck_run_end_unavailable_sequences",
        ),
        schema="lab",
    )
    op.execute(
        "REVOKE ALL ON lab.holdout_run_end_unavailable FROM PUBLIC, "
        "swapp_lab_director, swapp_lab_scorer, swapp_lab_planner"
    )
    op.execute(
        """
        CREATE FUNCTION lab.read_run_end_admission_registration(p_run_id uuid)
        RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'Director role required';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM lab.runs WHERE run_id=p_run_id) THEN
                RAISE EXCEPTION 'run is unavailable';
            END IF;
            RETURN jsonb_build_object(
                'intent_registered', EXISTS (SELECT 1 FROM lab.holdout_run_end_intents
                                              WHERE run_id=p_run_id),
                'reservation_exists', EXISTS (SELECT 1 FROM lab.holdout_reservations
                    WHERE run_id=p_run_id AND trigger_kind='run_end' AND trigger_index=1)
            );
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_run_end_unavailable()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE target_run uuid;
        BEGIN
            target_run := NEW.run_id;
            PERFORM 1 FROM lab.runs WHERE run_id=target_run FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'run is unavailable'; END IF;
            IF TG_TABLE_NAME = 'holdout_reservations' THEN
                IF NEW.trigger_kind <> 'run_end' THEN RETURN NEW; END IF;
            END IF;
            IF EXISTS (SELECT 1 FROM lab.holdout_run_end_unavailable WHERE run_id=target_run)
               THEN RAISE EXCEPTION 'run-end admission is durably unavailable'; END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER run_end_unavailable_blocks_experiments BEFORE INSERT ON lab.experiments "
        "FOR EACH ROW EXECUTE FUNCTION lab.guard_run_end_unavailable()"
    )
    op.execute(
        "CREATE TRIGGER run_end_unavailable_blocks_intents BEFORE INSERT "
        "ON lab.holdout_run_end_intents FOR EACH ROW "
        "EXECUTE FUNCTION lab.guard_run_end_unavailable()"
    )
    op.execute(
        "CREATE TRIGGER run_end_unavailable_blocks_reservations BEFORE INSERT "
        "ON lab.holdout_reservations FOR EACH ROW "
        "EXECUTE FUNCTION lab.guard_run_end_unavailable()"
    )
    op.execute(
        """
        CREATE FUNCTION lab.fence_run_end_unavailable(
            p_run_id uuid,p_candidate text,p_reason text,p_state_key text,p_state_sha text,
            p_state_seq integer,p_budget_sha text,p_checkpoint_sha text,p_checkpoint_seq integer,
            p_remaining_wall double precision,p_intent_sha text,p_intent_seq integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE run_item lab.runs%ROWTYPE; existing lab.holdout_run_end_unavailable%ROWTYPE;
                state_event jsonb; unavailable_event jsonb; intent_event jsonb;
                request_wall integer;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'Director role required';
            END IF;
            SELECT * INTO run_item FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
            IF NOT FOUND OR run_item.state <> 'running' OR run_item.stop_requested THEN
                RAISE EXCEPTION 'run is not active';
            END IF;
            IF p_reason IS NULL OR p_reason NOT IN (
                'fresh_wall_budget_unavailable', 'intent_checkpoint_without_sql_registration'
            ) THEN
                RAISE EXCEPTION 'run-end unavailable reason is invalid';
            END IF;
            IF p_candidate IS NULL OR length(p_candidate)=0 OR
               p_state_key IS NULL OR length(p_state_key)=0 OR
               p_state_sha !~ '^[0-9a-f]{64}$' OR p_budget_sha !~ '^[0-9a-f]{64}$' OR
               p_checkpoint_sha !~ '^[0-9a-f]{64}$' OR
               p_state_seq < 0 OR p_checkpoint_seq <= p_state_seq OR
               p_remaining_wall IS NULL OR p_remaining_wall < 0 OR
               p_remaining_wall::text IN ('NaN','Infinity','-Infinity') THEN
                RAISE EXCEPTION 'run-end unavailable identity is malformed';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM lab.experiments WHERE run_id=p_run_id
                           AND experiment_id=p_candidate AND status='scored') THEN
                RAISE EXCEPTION 'run-end unavailable candidate is not a scored run candidate';
            END IF;
            SELECT event_json INTO state_event FROM lab.run_events WHERE run_id=p_run_id
             AND event_type='director.checkpoint' AND event_json->>'key'=p_state_key;
            IF state_event IS NULL OR state_event->>'phase' IS DISTINCT FROM 'director_loop_state'
               OR state_event->>'payload_sha256' IS DISTINCT FROM p_state_sha
               OR (state_event->>'sequence')::integer IS DISTINCT FROM p_state_seq THEN
                RAISE EXCEPTION 'run-end unavailable prior state receipt differs';
            END IF;
            SELECT event_json INTO unavailable_event FROM lab.run_events WHERE run_id=p_run_id
             AND event_type='director.checkpoint'
             AND event_json->>'key'='director-holdout-run-end-unavailable-intent:1';
            IF unavailable_event IS NULL
               OR unavailable_event->>'phase' IS DISTINCT FROM 'holdout_run_end_unavailable_intent'
               OR unavailable_event->>'payload_sha256' IS DISTINCT FROM p_checkpoint_sha
               OR (unavailable_event->>'sequence')::integer IS DISTINCT FROM p_checkpoint_seq THEN
                RAISE EXCEPTION 'run-end unavailable checkpoint receipt differs';
            END IF;
            IF EXISTS (SELECT 1 FROM lab.holdout_run_end_intents WHERE run_id=p_run_id)
               OR EXISTS (SELECT 1 FROM lab.holdout_reservations WHERE run_id=p_run_id
                          AND trigger_kind='run_end' AND trigger_index=1) THEN
                RAISE EXCEPTION 'run-end SQL admission already exists';
            END IF;
            IF p_reason='fresh_wall_budget_unavailable' THEN
                request_wall := (run_item.request_json->'budget'->>'wall_seconds')::integer;
                IF request_wall IS NULL OR request_wall < 1 OR p_remaining_wall >= 1
                   OR p_intent_sha IS NOT NULL OR p_intent_seq IS NOT NULL THEN
                    RAISE EXCEPTION 'fresh wall exhaustion fence is not valid';
                END IF;
            ELSE
                SELECT event_json INTO intent_event FROM lab.run_events WHERE run_id=p_run_id
                 AND event_type='director.checkpoint'
                 AND event_json->>'key'='director-holdout-run-end-intent';
                IF p_intent_sha !~ '^[0-9a-f]{64}$' OR p_intent_seq IS NULL OR p_intent_seq < 0
                   OR intent_event IS NULL
                   OR intent_event->>'phase' IS DISTINCT FROM 'holdout_run_end_intent'
                   OR intent_event->>'payload_sha256' IS DISTINCT FROM p_intent_sha
                   OR (intent_event->>'sequence')::integer IS DISTINCT FROM p_intent_seq
                   OR intent_event->>'sequence' IS NULL THEN
                    RAISE EXCEPTION 'original run-end intent checkpoint is unavailable';
                END IF;
            END IF;
            SELECT * INTO existing FROM lab.holdout_run_end_unavailable WHERE run_id=p_run_id;
            IF FOUND THEN
                IF ROW(existing.candidate_experiment_id,existing.reason,existing.prior_state_key,
                       existing.prior_state_sha256,existing.prior_state_sequence,
                       existing.budget_snapshot_sha256,existing.unavailable_checkpoint_sha256,
                       existing.unavailable_checkpoint_sequence,
                       existing.original_intent_checkpoint_sha256,
                       existing.original_intent_checkpoint_sequence) IS DISTINCT FROM
                   ROW(p_candidate,p_reason,p_state_key,p_state_sha,p_state_seq,p_budget_sha,
                       p_checkpoint_sha,p_checkpoint_seq,p_intent_sha,p_intent_seq) THEN
                    RAISE EXCEPTION 'run-end unavailable fence identity changed';
                END IF;
                RETURN jsonb_build_object('run_id',p_run_id,
                    'candidate_experiment_id',existing.candidate_experiment_id,
                    'reason',existing.reason,'prior_state_key',existing.prior_state_key,
                    'prior_state_sha256',existing.prior_state_sha256,
                    'prior_state_sequence',existing.prior_state_sequence,
                    'budget_snapshot_sha256',existing.budget_snapshot_sha256,
                    'unavailable_checkpoint_sha256',existing.unavailable_checkpoint_sha256,
                    'unavailable_checkpoint_sequence',existing.unavailable_checkpoint_sequence,
                    'original_intent_checkpoint_sha256',existing.original_intent_checkpoint_sha256,
                    'original_intent_checkpoint_sequence',existing.original_intent_checkpoint_sequence,
                    'state','failed','bit',NULL);
            END IF;
            INSERT INTO lab.holdout_run_end_unavailable(run_id,candidate_experiment_id,reason,
                prior_state_key,prior_state_sha256,prior_state_sequence,budget_snapshot_sha256,
                unavailable_checkpoint_sha256,unavailable_checkpoint_sequence,
                original_intent_checkpoint_sha256,original_intent_checkpoint_sequence)
            VALUES (p_run_id,p_candidate,p_reason,p_state_key,p_state_sha,p_state_seq,p_budget_sha,
                p_checkpoint_sha,p_checkpoint_seq,p_intent_sha,p_intent_seq);
            RETURN jsonb_build_object('run_id',p_run_id,'candidate_experiment_id',p_candidate,
                'reason',p_reason,'prior_state_key',p_state_key,'prior_state_sha256',p_state_sha,
                'prior_state_sequence',p_state_seq,'budget_snapshot_sha256',p_budget_sha,
                'unavailable_checkpoint_sha256',p_checkpoint_sha,
                'unavailable_checkpoint_sequence',p_checkpoint_seq,
                'original_intent_checkpoint_sha256',p_intent_sha,
                'original_intent_checkpoint_sequence',p_intent_seq,'state','failed','bit',NULL);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.read_run_end_unavailable(p_run_id uuid)
        RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE item lab.holdout_run_end_unavailable%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'Director role required';
            END IF;
            SELECT * INTO item FROM lab.holdout_run_end_unavailable WHERE run_id=p_run_id;
            IF NOT FOUND THEN RETURN NULL; END IF;
            RETURN jsonb_build_object('run_id',item.run_id,
                'candidate_experiment_id',item.candidate_experiment_id,'reason',item.reason,
                'prior_state_key',item.prior_state_key,'prior_state_sha256',item.prior_state_sha256,
                'prior_state_sequence',item.prior_state_sequence,
                'budget_snapshot_sha256',item.budget_snapshot_sha256,
                'unavailable_checkpoint_sha256',item.unavailable_checkpoint_sha256,
                'unavailable_checkpoint_sequence',item.unavailable_checkpoint_sequence,
                'original_intent_checkpoint_sha256',item.original_intent_checkpoint_sha256,
                'original_intent_checkpoint_sequence',item.original_intent_checkpoint_sequence,
                'state','failed','bit',NULL);
        END;
        $$
        """
    )
    signatures = (
        "lab.fence_run_end_unavailable("
        "uuid,text,text,text,text,integer,text,text,integer,double precision,text,integer)",
        "lab.read_run_end_unavailable(uuid)",
        "lab.read_run_end_admission_registration(uuid)",
    )
    for signature in signatures:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO swapp_lab_director")
    op.execute("REVOKE ALL ON FUNCTION lab.guard_run_end_unavailable() FROM PUBLIC")


def downgrade() -> None:
    """Remove only the unavailable fence and its admission guards."""
    op.execute("DROP TRIGGER run_end_unavailable_blocks_experiments ON lab.experiments")
    op.execute("DROP TRIGGER run_end_unavailable_blocks_intents ON lab.holdout_run_end_intents")
    op.execute("DROP TRIGGER run_end_unavailable_blocks_reservations ON lab.holdout_reservations")
    for signature in (
        "lab.read_run_end_unavailable(uuid)",
        "lab.fence_run_end_unavailable("
        "uuid,text,text,text,text,integer,text,text,integer,double precision,text,integer)",
        "lab.read_run_end_admission_registration(uuid)",
    ):
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("DROP FUNCTION lab.guard_run_end_unavailable()")
    op.drop_table("holdout_run_end_unavailable", schema="lab")
