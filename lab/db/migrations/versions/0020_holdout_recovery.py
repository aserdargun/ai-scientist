"""Add Scorer-only admission and drained-worker recovery RPCs."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_holdout_recovery"
down_revision = "0019_bounded_holdout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Expose only the worker identity and run admission needed for recovery."""
    op.create_table(
        "holdout_run_end_fences",
        sa.Column("run_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("candidate_experiment_id", sa.String(128), nullable=False),
        sa.Column("intent_checkpoint_sha256", sa.String(64), nullable=False),
        sa.Column("intent_checkpoint_sequence", sa.Integer, nullable=False),
        sa.Column("budget_checkpoint_key", sa.String(160), nullable=False),
        sa.Column("budget_checkpoint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "failure_kind",
            sa.String(48),
            nullable=False,
            server_default="missing_reservation_unverifiable_budget",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["run_id"], ["lab.holdout_run_end_intents.run_id"]),
        sa.ForeignKeyConstraint(["candidate_experiment_id"], ["lab.experiments.experiment_id"]),
        sa.CheckConstraint(
            "intent_checkpoint_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_holdout_fence_intent_sha",
        ),
        sa.CheckConstraint(
            "budget_checkpoint_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_holdout_fence_budget_sha",
        ),
        sa.CheckConstraint("intent_checkpoint_sequence > 0", name="ck_holdout_fence_sequence"),
        sa.CheckConstraint(
            "failure_kind='missing_reservation_unverifiable_budget'",
            name="ck_holdout_fence_failure_kind",
        ),
        schema="lab",
    )
    op.execute(
        "REVOKE ALL ON lab.holdout_run_end_fences FROM PUBLIC, "
        "swapp_lab_director, swapp_lab_scorer, swapp_lab_planner"
    )
    op.execute(
        """
        CREATE FUNCTION lab.read_holdout_recovery_target(p_reservation_id uuid)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE item lab.holdout_reservations%ROWTYPE; target_run uuid;
                run_item lab.runs%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'Scorer role required';
            END IF;
            SELECT run_id INTO target_run FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout reservation is unavailable'; END IF;
            SELECT * INTO run_item FROM lab.runs WHERE run_id=target_run FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout run is unavailable'; END IF;
            SELECT * INTO item FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id FOR UPDATE;
            IF NOT FOUND OR item.run_id <> run_item.run_id THEN
                RAISE EXCEPTION 'holdout reservation identity changed';
            END IF;
            RETURN jsonb_build_object(
                'reservation_id',item.reservation_id,
                'run_id',item.run_id,
                'run_state',run_item.state,
                'stop_requested',run_item.stop_requested,
                'state',item.state,
                'worker_pid',item.worker_pid,
                'worker_start_ticks',item.worker_start_ticks,
                'worker_boot_id',item.worker_boot_id,
                'worker_unit',item.worker_unit,
                'worker_invocation_id',item.worker_invocation_id,
                'worker_cgroup',item.worker_cgroup
            );
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.check_holdout_admission(
            p_reservation_id uuid,p_run_id uuid,p_worker_pid integer,p_start_ticks bigint,
            p_boot_id text,p_unit text,p_invocation_id text,p_cgroup text
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE target_run uuid; run_item lab.runs%ROWTYPE; item lab.holdout_reservations%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'Scorer role required';
            END IF;
            SELECT run_id INTO target_run FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id;
            IF NOT FOUND OR target_run <> p_run_id THEN RETURN false; END IF;
            SELECT * INTO run_item FROM lab.runs WHERE run_id=target_run FOR UPDATE;
            IF NOT FOUND OR run_item.state <> 'running' OR run_item.stop_requested THEN
                RETURN false;
            END IF;
            SELECT * INTO item FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id FOR UPDATE;
            RETURN FOUND AND item.run_id=target_run AND item.state='running' AND
                ROW(item.worker_pid,item.worker_start_ticks,item.worker_boot_id,item.worker_unit,
                    item.worker_invocation_id,item.worker_cgroup) IS NOT DISTINCT FROM
                ROW(p_worker_pid,p_start_ticks,p_boot_id,p_unit,p_invocation_id,p_cgroup);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.recover_holdout_failure(
            p_reservation_id uuid,p_run_id uuid,p_worker_pid integer,p_start_ticks bigint,
            p_boot_id text,p_unit text,p_invocation_id text,p_cgroup text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE target_run uuid; run_item lab.runs%ROWTYPE; item lab.holdout_reservations%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'Scorer role required';
            END IF;
            SELECT run_id INTO target_run FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout reservation is unavailable'; END IF;
            SELECT * INTO run_item FROM lab.runs WHERE run_id=target_run FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout run is unavailable'; END IF;
            SELECT * INTO item FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id FOR UPDATE;
            IF NOT FOUND OR item.run_id <> target_run OR item.run_id <> p_run_id THEN
                RAISE EXCEPTION 'holdout reservation identity changed';
            END IF;
            IF item.state IN ('passed','reverted','failed','exhausted') THEN
                RETURN jsonb_build_object('reservation_id',item.reservation_id,
                    'state',item.state,'bit',CASE WHEN item.state IN ('passed','reverted')
                    THEN item.result_bit ELSE NULL END);
            END IF;
            IF item.state <> 'running' OR
               ROW(item.worker_pid,item.worker_start_ticks,item.worker_boot_id,item.worker_unit,
                   item.worker_invocation_id,item.worker_cgroup) IS DISTINCT FROM
               ROW(p_worker_pid,p_start_ticks,p_boot_id,p_unit,p_invocation_id,p_cgroup) THEN
                RAISE EXCEPTION 'holdout worker generation is stale';
            END IF;
            UPDATE lab.holdout_reservations SET state='failed',result_bit=NULL,
                error_code='worker_recovered',completed_at=now()
             WHERE reservation_id=p_reservation_id AND state='running';
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout reservation recovery lost its CAS'; END IF;
            RETURN jsonb_build_object('reservation_id',item.reservation_id,
                'state','failed','bit',NULL);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.recover_unclaimed_holdout_failure(
            p_reservation_id uuid,p_run_id uuid
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE target_run uuid; run_item lab.runs%ROWTYPE;
                item lab.holdout_reservations%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'Scorer role required';
            END IF;
            SELECT run_id INTO target_run FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout reservation is unavailable'; END IF;
            SELECT * INTO run_item FROM lab.runs WHERE run_id=target_run FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout run is unavailable'; END IF;
            SELECT * INTO item FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id FOR UPDATE;
            IF NOT FOUND OR item.run_id <> p_run_id OR item.run_id <> run_item.run_id THEN
                RAISE EXCEPTION 'holdout reservation identity changed';
            END IF;
            IF item.state IN ('passed','reverted','failed','exhausted') THEN
                RETURN jsonb_build_object('reservation_id',item.reservation_id,
                    'state',item.state,'bit',CASE WHEN item.state IN ('passed','reverted')
                    THEN item.result_bit ELSE NULL END);
            END IF;
            IF item.state <> 'reserved' THEN
                RETURN jsonb_build_object('reservation_id',item.reservation_id,
                    'state','pending','bit',NULL);
            END IF;
            UPDATE lab.holdout_reservations SET state='failed',result_bit=NULL,
                result_sha256=NULL,error_code='worker_never_claimed',completed_at=now()
             WHERE reservation_id=p_reservation_id AND run_id=p_run_id AND state='reserved';
            IF NOT FOUND THEN RAISE EXCEPTION 'unclaimed holdout recovery lost its CAS'; END IF;
            RETURN jsonb_build_object('reservation_id',item.reservation_id,
                'state','failed','bit',NULL);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.list_holdout_recovery_targets(p_run_id uuid)
        RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE run_item lab.runs%ROWTYPE; targets jsonb;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'Scorer role required';
            END IF;
            SELECT * INTO run_item FROM lab.runs WHERE run_id=p_run_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout run is unavailable'; END IF;
            SELECT COALESCE(jsonb_agg(jsonb_build_object(
                'reservation_id',reservation_id,'state',state
            ) ORDER BY created_at,reservation_id),'[]'::jsonb)
              INTO targets FROM lab.holdout_reservations
             WHERE run_id=p_run_id AND state IN ('reserved','running');
            RETURN targets;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_holdout_run_end_fence()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE target_run uuid;
        BEGIN
            IF NEW.trigger_kind <> 'run_end' THEN RETURN NEW; END IF;
            target_run := NEW.run_id;
            PERFORM 1 FROM lab.runs WHERE run_id=target_run FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout run is unavailable'; END IF;
            IF EXISTS (SELECT 1 FROM lab.holdout_run_end_fences WHERE run_id=target_run) THEN
                RAISE EXCEPTION 'run-end holdout is fenced after an unrecoverable gap';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER holdout_run_end_fence_guard BEFORE INSERT ON lab.holdout_reservations "
        "FOR EACH ROW EXECUTE FUNCTION lab.guard_holdout_run_end_fence()"
    )
    op.execute(
        """
        CREATE FUNCTION lab.fence_missing_holdout_run_end(
            p_run_id uuid,p_intent_checkpoint_sha256 text,p_intent_checkpoint_sequence integer,
            p_budget_checkpoint_key text,p_budget_checkpoint_sha256 text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE run_item lab.runs%ROWTYPE; intent lab.holdout_run_end_intents%ROWTYPE;
                existing lab.holdout_run_end_fences%ROWTYPE; budget_event jsonb;
                budget_reservation_id uuid; reserved_event jsonb;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'Director role required';
            END IF;
            SELECT * INTO run_item FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout run is unavailable'; END IF;
            SELECT * INTO intent FROM lab.holdout_run_end_intents
             WHERE run_id=p_run_id FOR UPDATE;
            IF NOT FOUND OR intent.intent_checkpoint_sha256 IS DISTINCT FROM
               p_intent_checkpoint_sha256 OR intent.intent_checkpoint_sequence IS DISTINCT FROM
               p_intent_checkpoint_sequence THEN
                RAISE EXCEPTION 'run-end intent checkpoint identity changed';
            END IF;
            IF p_intent_checkpoint_sha256 !~ '^[0-9a-f]{64}$'
               OR p_budget_checkpoint_sha256 !~ '^[0-9a-f]{64}$'
               OR p_intent_checkpoint_sequence <= 0
               OR p_budget_checkpoint_key !~ '^holdout-budget-reconciled:[0-9a-f-]{36}$' THEN
                RAISE EXCEPTION 'run-end budget fence identity is malformed';
            END IF;
            budget_reservation_id := substring(
                p_budget_checkpoint_key from '^holdout-budget-reconciled:([0-9a-f-]{36})$'
            )::uuid;
            SELECT event_json INTO budget_event FROM lab.run_events
             WHERE run_id=p_run_id AND event_type='director.checkpoint'
               AND event_json->>'key'=p_budget_checkpoint_key;
            SELECT event_json INTO reserved_event FROM lab.run_events
             WHERE run_id=p_run_id AND event_type='director.checkpoint'
               AND event_json->>'key'='holdout-budget-reserved:' || budget_reservation_id::text;
            IF budget_event IS NULL OR budget_event->>'phase' IS DISTINCT FROM
               'holdout_budget_reconciled'
               OR budget_event->>'payload_sha256' IS DISTINCT FROM p_budget_checkpoint_sha256
               OR (budget_event->>'sequence')::integer <= p_intent_checkpoint_sequence
               OR reserved_event IS NULL OR reserved_event->>'phase' IS DISTINCT FROM
                   'holdout_budget_reserved'
               OR (reserved_event->>'sequence')::integer >= p_intent_checkpoint_sequence THEN
                RAISE EXCEPTION 'durable run-end budget reconciliation checkpoint is unavailable';
            END IF;
            IF EXISTS (SELECT 1 FROM lab.holdout_reservations WHERE run_id=p_run_id
                       AND trigger_kind='run_end' AND trigger_index=1) THEN
                RAISE EXCEPTION 'run-end holdout reservation already exists';
            END IF;
            SELECT * INTO existing FROM lab.holdout_run_end_fences WHERE run_id=p_run_id;
            IF FOUND THEN
                IF ROW(existing.candidate_experiment_id,existing.intent_checkpoint_sha256,
                       existing.intent_checkpoint_sequence,existing.budget_checkpoint_key,
                       existing.budget_checkpoint_sha256) IS DISTINCT FROM
                   ROW(intent.candidate_experiment_id,p_intent_checkpoint_sha256,
                       p_intent_checkpoint_sequence,p_budget_checkpoint_key,
                       p_budget_checkpoint_sha256) THEN
                    RAISE EXCEPTION 'run-end missing-reservation fence identity changed';
                END IF;
                RETURN jsonb_build_object('run_id',p_run_id,
                    'candidate_experiment_id',existing.candidate_experiment_id,
                    'intent_checkpoint_sha256',existing.intent_checkpoint_sha256,
                    'intent_checkpoint_sequence',existing.intent_checkpoint_sequence,
                    'budget_checkpoint_key',existing.budget_checkpoint_key,
                    'budget_checkpoint_sha256',existing.budget_checkpoint_sha256,
                    'state','failed','bit',NULL,'failure_kind',existing.failure_kind);
            END IF;
            INSERT INTO lab.holdout_run_end_fences(
                run_id,candidate_experiment_id,intent_checkpoint_sha256,
                intent_checkpoint_sequence,budget_checkpoint_key,budget_checkpoint_sha256,
                failure_kind
            ) VALUES (
                p_run_id,intent.candidate_experiment_id,p_intent_checkpoint_sha256,
                p_intent_checkpoint_sequence,p_budget_checkpoint_key,p_budget_checkpoint_sha256,
                'missing_reservation_unverifiable_budget'
            );
            RETURN jsonb_build_object('run_id',p_run_id,
                'candidate_experiment_id',intent.candidate_experiment_id,
                'intent_checkpoint_sha256',p_intent_checkpoint_sha256,
                'intent_checkpoint_sequence',p_intent_checkpoint_sequence,
                'budget_checkpoint_key',p_budget_checkpoint_key,
                'budget_checkpoint_sha256',p_budget_checkpoint_sha256,
                'state','failed','bit',NULL,
                'failure_kind','missing_reservation_unverifiable_budget');
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.read_holdout_run_end_fence(p_run_id uuid)
        RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE item lab.holdout_run_end_fences%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'Director role required';
            END IF;
            SELECT * INTO item FROM lab.holdout_run_end_fences WHERE run_id=p_run_id;
            IF NOT FOUND THEN RETURN NULL; END IF;
            RETURN jsonb_build_object('run_id',item.run_id,
                'candidate_experiment_id',item.candidate_experiment_id,
                'intent_checkpoint_sha256',item.intent_checkpoint_sha256,
                'intent_checkpoint_sequence',item.intent_checkpoint_sequence,
                'budget_checkpoint_key',item.budget_checkpoint_key,
                'budget_checkpoint_sha256',item.budget_checkpoint_sha256,
                'state','failed','bit',NULL,'failure_kind',item.failure_kind);
        END;
        $$
        """
    )
    director_functions = {
        "lab.fence_missing_holdout_run_end(uuid,text,integer,text,text)",
        "lab.read_holdout_run_end_fence(uuid)",
    }
    for signature in (
        "lab.read_holdout_recovery_target(uuid)",
        "lab.check_holdout_admission(uuid,uuid,integer,bigint,text,text,text,text)",
        "lab.recover_holdout_failure(uuid,uuid,integer,bigint,text,text,text,text)",
        "lab.recover_unclaimed_holdout_failure(uuid,uuid)",
        "lab.list_holdout_recovery_targets(uuid)",
        "lab.fence_missing_holdout_run_end(uuid,text,integer,text,text)",
        "lab.read_holdout_run_end_fence(uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        role = "swapp_lab_director" if signature in director_functions else "swapp_lab_scorer"
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {role}")
    op.execute("REVOKE ALL ON FUNCTION lab.guard_holdout_run_end_fence() FROM PUBLIC")


def downgrade() -> None:
    """Remove recovery RPCs without changing prior holdout records."""
    op.execute("DROP TRIGGER holdout_run_end_fence_guard ON lab.holdout_reservations")
    op.execute("DROP FUNCTION lab.guard_holdout_run_end_fence()")
    for signature in (
        "lab.recover_holdout_failure(uuid,uuid,integer,bigint,text,text,text,text)",
        "lab.recover_unclaimed_holdout_failure(uuid,uuid)",
        "lab.list_holdout_recovery_targets(uuid)",
        "lab.check_holdout_admission(uuid,uuid,integer,bigint,text,text,text,text)",
        "lab.read_holdout_recovery_target(uuid)",
        "lab.read_holdout_run_end_fence(uuid)",
        "lab.fence_missing_holdout_run_end(uuid,text,integer,text,text)",
    ):
        op.execute(f"DROP FUNCTION {signature}")
    op.drop_table("holdout_run_end_fences", schema="lab")
