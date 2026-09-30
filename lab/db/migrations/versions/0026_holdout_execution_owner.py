"""Bind first-generation holdout work and receipts to its immutable owner."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_holdout_execution_owner"
down_revision = "0025_director_generations"
branch_labels = None
depends_on = None
_TABLES = (
    ("holdout_reservations", "lab"),
    ("holdout_run_end_intents", "lab"),
    ("holdout_run_end_fences", "lab"),
    ("holdout_run_end_unavailable", "lab"),
)


def upgrade() -> None:
    """Require new holdout operations to carry the run's captured owner pair."""
    op.create_unique_constraint(
        "uq_director_generation_execution_pair",
        "director_owner_generations",
        ["run_id", "generation", "execution_sha256"],
        schema="lab",
    )
    op.add_column(
        "holdout_run_end_unavailable",
        sa.Column("reservation_checkpoint_key", sa.String(160)),
        schema="lab",
    )
    op.add_column(
        "holdout_run_end_unavailable",
        sa.Column("reservation_checkpoint_sha256", sa.String(64)),
        schema="lab",
    )
    op.add_column(
        "holdout_run_end_unavailable",
        sa.Column("reservation_checkpoint_sequence", sa.Integer()),
        schema="lab",
    )
    op.drop_constraint("ck_run_end_unavailable_reason", "holdout_run_end_unavailable", schema="lab")
    op.drop_constraint(
        "ck_run_end_unavailable_reason_intent",
        "holdout_run_end_unavailable",
        schema="lab",
    )
    op.create_check_constraint(
        "ck_run_end_unavailable_reason",
        "holdout_run_end_unavailable",
        "reason in ('fresh_wall_budget_unavailable', "
        "'intent_checkpoint_without_sql_registration', "
        "'reservation_checkpoint_without_sql_intent')",
        schema="lab",
    )
    op.create_check_constraint(
        "ck_run_end_unavailable_reason_intent",
        "holdout_run_end_unavailable",
        "(reason='fresh_wall_budget_unavailable' and original_intent_checkpoint_sha256 is null) "
        "or (reason='intent_checkpoint_without_sql_registration' and "
        "original_intent_checkpoint_sha256 is not null) "
        "or (reason='reservation_checkpoint_without_sql_intent' and "
        "original_intent_checkpoint_sha256 is null)",
        schema="lab",
    )
    op.create_check_constraint(
        "ck_run_end_unavailable_reservation_checkpoint",
        "holdout_run_end_unavailable",
        "(reason='reservation_checkpoint_without_sql_intent' and "
        "reservation_checkpoint_key is not null and "
        "reservation_checkpoint_sha256 is not null and "
        "reservation_checkpoint_sha256 ~ '^[0-9a-f]{64}$' and "
        "reservation_checkpoint_sequence is not null and "
        "reservation_checkpoint_sequence >= 0) or "
        "(reason<>'reservation_checkpoint_without_sql_intent' and "
        "reservation_checkpoint_key is null and reservation_checkpoint_sha256 is null and "
        "reservation_checkpoint_sequence is null)",
        schema="lab",
    )
    for table, schema in _TABLES:
        op.add_column(table, sa.Column("admitted_generation", sa.Integer()), schema=schema)
        op.add_column(table, sa.Column("execution_sha256", sa.String(64)), schema=schema)
        op.create_check_constraint(
            f"ck_{table}_owner_pair",
            table,
            "(admitted_generation IS NULL AND execution_sha256 IS NULL) OR "
            "(admitted_generation IS NOT NULL AND admitted_generation > 0 AND "
            "execution_sha256 IS NOT NULL AND execution_sha256 ~ '^[0-9a-f]{64}$')",
            schema=schema,
        )
        op.create_foreign_key(
            f"fk_{table}_owner_pair",
            table,
            "director_owner_generations",
            ["run_id", "admitted_generation", "execution_sha256"],
            ["run_id", "generation", "execution_sha256"],
            source_schema=schema,
            referent_schema="lab",
            ondelete="RESTRICT",
        )
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM lab.holdout_reservations
                 WHERE state IN ('reserved','running')
                   AND (admitted_generation IS NULL OR execution_sha256 IS NULL)
            ) THEN
                RAISE EXCEPTION
                    'active legacy holdout reservations need drained cleanup before 0026';
            END IF;
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.assert_director_holdout_execution(
            p_run_id uuid,p_generation integer,p_invocation_id text,p_execution_sha256 text
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE run_item lab.runs%ROWTYPE; control_item lab.director_execution_control%ROWTYPE;
            contract_item lab.director_execution_contracts%ROWTYPE; lock_key bigint;
        BEGIN
            IF session_user <> 'swapp_lab_director' OR p_run_id IS NULL OR
               p_generation IS NULL OR p_generation < 1 OR
               p_invocation_id IS NULL OR p_invocation_id !~ '^[0-9a-f]{32}$' OR
               p_execution_sha256 IS NULL OR p_execution_sha256 !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'Director holdout owner identity is malformed';
            END IF;
            lock_key := ('x' || substr(encode(sha256(uuid_send(p_run_id)), 'hex'), 1, 16))
                ::bit(64)::bigint;
            PERFORM pg_advisory_xact_lock(lock_key);
            SELECT * INTO run_item FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
            SELECT * INTO control_item FROM lab.director_execution_control
             WHERE run_id=p_run_id FOR UPDATE;
            SELECT * INTO contract_item FROM lab.director_execution_contracts
             WHERE run_id=p_run_id;
            IF run_item.run_id IS NULL OR run_item.state <> 'running' OR
               run_item.stop_requested OR control_item.run_id IS NULL OR
               control_item.mode <> 'active' OR
               control_item.current_generation IS DISTINCT FROM p_generation OR
               contract_item.run_id IS NULL OR
               contract_item.execution_sha256 IS DISTINCT FROM p_execution_sha256 OR
               contract_item.deadline_at <= clock_timestamp() OR NOT EXISTS (
                   SELECT 1 FROM lab.director_owner_generations owner
                    WHERE owner.run_id=p_run_id AND owner.generation=p_generation
                      AND owner.worker_invocation_id=p_invocation_id
                      AND owner.execution_sha256=p_execution_sha256
               ) THEN
                RAISE EXCEPTION 'Director holdout owner is no longer active';
            END IF;
            PERFORM set_config('lab.holdout_run_id',p_run_id::text,true);
            PERFORM set_config('lab.holdout_generation',p_generation::text,true);
            PERFORM set_config('lab.holdout_execution_sha256',p_execution_sha256,true);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.assert_director_holdout_closure(
            p_run_id uuid,p_generation integer,p_invocation_id text,p_execution_sha256 text
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE run_item lab.runs%ROWTYPE; control_item lab.director_execution_control%ROWTYPE;
            contract_item lab.director_execution_contracts%ROWTYPE; lock_key bigint;
        BEGIN
            IF session_user <> 'swapp_lab_director' OR p_run_id IS NULL OR
               p_generation IS NULL OR p_generation < 1 OR
               p_invocation_id IS NULL OR p_invocation_id !~ '^[0-9a-f]{32}$' OR
               p_execution_sha256 IS NULL OR p_execution_sha256 !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'Director holdout closure identity is malformed';
            END IF;
            lock_key := ('x' || substr(encode(sha256(uuid_send(p_run_id)), 'hex'), 1, 16))
                ::bit(64)::bigint;
            PERFORM pg_advisory_xact_lock(lock_key);
            SELECT * INTO run_item FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
            SELECT * INTO control_item FROM lab.director_execution_control
             WHERE run_id=p_run_id FOR UPDATE;
            SELECT * INTO contract_item FROM lab.director_execution_contracts
             WHERE run_id=p_run_id;
            IF run_item.run_id IS NULL OR run_item.state NOT IN
                   ('running','stop_requested','stopped') OR
               control_item.run_id IS NULL OR control_item.mode <> 'active' OR
               control_item.current_generation IS DISTINCT FROM p_generation OR
               contract_item.run_id IS NULL OR
               contract_item.execution_sha256 IS DISTINCT FROM p_execution_sha256 OR
               contract_item.payload_sha256 IS DISTINCT FROM run_item.payload_sha256 OR
               NOT EXISTS (
                   SELECT 1 FROM lab.director_owner_generations owner
                    WHERE owner.run_id=p_run_id AND owner.generation=p_generation
                      AND owner.worker_invocation_id=p_invocation_id
                      AND owner.execution_sha256=p_execution_sha256
               ) THEN
                RAISE EXCEPTION 'Director holdout closure owner is no longer current';
            END IF;
            PERFORM set_config('lab.holdout_run_id',p_run_id::text,true);
            PERFORM set_config('lab.holdout_generation',p_generation::text,true);
            PERFORM set_config('lab.holdout_execution_sha256',p_execution_sha256,true);
            PERFORM set_config('lab.holdout_closure_run_id',p_run_id::text,true);
            PERFORM set_config('lab.holdout_closure_generation',p_generation::text,true);
            PERFORM set_config('lab.holdout_closure_execution_sha256',p_execution_sha256,true);
        END;
        $$
        """
    )
    # The two bitless fence tables permit only exact same-generation closure.
    # All ordinary writes retain the original active-owner assertion.
    op.execute(
        """
        CREATE FUNCTION lab.guard_director_holdout_fence_statement()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE owner_run uuid; owner_generation integer; owner_invocation text;
            owner_execution text;
        BEGIN
            IF session_user='swapp_lab_migrator' THEN RETURN NULL; END IF;
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'Director role required for holdout fence mutation';
            END IF;
            owner_run := nullif(current_setting('lab.owner_run_id',true),'')::uuid;
            owner_generation := nullif(
                current_setting('lab.owner_generation',true),'')::integer;
            owner_invocation := nullif(current_setting('lab.owner_invocation_id',true),'');
            owner_execution := nullif(
                current_setting('lab.owner_execution_sha256',true),'');
            IF owner_run IS NULL OR owner_generation IS NULL OR owner_invocation IS NULL OR
               owner_execution IS NULL THEN
                RAISE EXCEPTION 'Director holdout fence has no captured owner';
            END IF;
            IF current_setting('lab.holdout_closure_run_id',true)=owner_run::text AND
               current_setting('lab.holdout_closure_generation',true)=owner_generation::text AND
               current_setting('lab.holdout_closure_execution_sha256',true)=owner_execution THEN
                PERFORM lab.assert_director_holdout_closure(
                    owner_run,owner_generation,owner_invocation,owner_execution);
            ELSE
                PERFORM lab.assert_director_owner_context(owner_run);
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_director_holdout_fence_row()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE owner_run uuid; owner_generation integer; owner_invocation text;
            owner_execution text;
        BEGIN
            IF session_user='swapp_lab_migrator' THEN RETURN NEW; END IF;
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'Director role required for holdout fence mutation';
            END IF;
            owner_run := nullif(current_setting('lab.owner_run_id',true),'')::uuid;
            owner_generation := nullif(
                current_setting('lab.owner_generation',true),'')::integer;
            owner_invocation := nullif(current_setting('lab.owner_invocation_id',true),'');
            owner_execution := nullif(
                current_setting('lab.owner_execution_sha256',true),'');
            IF owner_run IS NULL OR owner_generation IS NULL OR owner_invocation IS NULL OR
               owner_execution IS NULL OR NEW.run_id IS DISTINCT FROM owner_run THEN
                RAISE EXCEPTION 'Director holdout fence row differs from captured owner';
            END IF;
            IF current_setting('lab.holdout_closure_run_id',true)=owner_run::text AND
               current_setting('lab.holdout_closure_generation',true)=owner_generation::text AND
               current_setting('lab.holdout_closure_execution_sha256',true)=owner_execution THEN
                PERFORM lab.assert_director_holdout_closure(
                    owner_run,owner_generation,owner_invocation,owner_execution);
            ELSE
                PERFORM lab.assert_director_owner_context(owner_run);
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table in ("holdout_run_end_unavailable", "holdout_run_end_fences"):
        trigger_prefix = f"lab_{table}"
        op.execute(f"DROP TRIGGER {trigger_prefix}_owner_fence ON lab.{table}")
        op.execute(f"DROP TRIGGER {trigger_prefix}_owner_row_fence ON lab.{table}")
        op.execute(
            f"CREATE TRIGGER {trigger_prefix}_owner_fence BEFORE INSERT ON lab.{table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION lab.guard_director_holdout_fence_statement()"
        )
        op.execute(
            f"CREATE TRIGGER {trigger_prefix}_owner_row_fence BEFORE INSERT ON lab.{table} "
            "FOR EACH ROW EXECUTE FUNCTION lab.guard_director_holdout_fence_row()"
        )
    op.execute(
        """
        CREATE FUNCTION lab.assert_director_holdout_checkpoint(
            p_run_id uuid,p_generation integer,p_invocation_id text,p_execution_sha256 text,
            p_key text,p_phase text,p_sequence integer,p_payload_sha256 text,p_metadata jsonb
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE expected_phase text; reservation_key text; reservation_event jsonb;
            reconciliation_event jsonb; previous_event jsonb; application_event jsonb;
            generic_meta jsonb; state_meta jsonb;
            reservation_meta jsonb; reconciliation_meta jsonb; freeze_meta jsonb;
            run_item lab.runs%ROWTYPE; contract_item lab.director_execution_contracts%ROWTYPE;
            unavailable_item lab.holdout_run_end_unavailable%ROWTYPE;
            reservation_item lab.holdout_reservations%ROWTYPE; app_meta jsonb;
        BEGIN
            IF session_user <> 'swapp_lab_director' OR p_key IS NULL OR p_phase IS NULL OR
               p_sequence IS NULL OR p_sequence < 0 OR
               p_payload_sha256 IS NULL OR p_payload_sha256 !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'Director holdout closure checkpoint identity is malformed';
            END IF;
            PERFORM lab.assert_director_holdout_closure(
                p_run_id,p_generation,p_invocation_id,p_execution_sha256);
            SELECT event_json INTO previous_event FROM lab.run_events
             WHERE run_id=p_run_id AND event_type='director.checkpoint'
               AND event_json->>'key'=p_key;
            IF FOUND THEN
                IF previous_event IS DISTINCT FROM p_metadata OR
                   previous_event->>'payload_sha256' IS DISTINCT FROM p_payload_sha256 OR
                   previous_event->>'phase' IS DISTINCT FROM p_phase OR
                   (previous_event->>'sequence')::integer IS DISTINCT FROM p_sequence THEN
                    RAISE EXCEPTION
                        'holdout closure checkpoint retry changed its immutable receipt';
                END IF;
            ELSE
            IF p_metadata->'holdout_closure_owner'->>'admitted_generation'
                   IS DISTINCT FROM p_generation::text OR
               p_metadata->'holdout_closure_owner'->>'execution_sha256'
                   IS DISTINCT FROM p_execution_sha256 THEN
                RAISE EXCEPTION 'holdout closure checkpoint lacks its exact owner metadata';
            END IF;
            IF p_phase IN ('holdout_application','holdout_run_end_unavailable',
                           'holdout_run_end_admission_failure') THEN
                generic_meta := p_metadata->'holdout_closure_application';
                IF generic_meta IS NULL OR
                   generic_meta->>'run_id' IS DISTINCT FROM p_run_id::text OR
                   generic_meta->>'admitted_generation' IS DISTINCT FROM p_generation::text OR
                   generic_meta->>'execution_sha256' IS DISTINCT FROM p_execution_sha256 OR
                   generic_meta->>'state' IS DISTINCT FROM 'failed' OR
                   generic_meta->'bit' IS DISTINCT FROM 'null'::jsonb OR
                   generic_meta->>'expected_state_sha256' IS NULL OR
                   generic_meta->>'expected_state_sha256' !~ '^[0-9a-f]{64}$' OR
                   generic_meta->>'budget_snapshot_sha256' IS NULL OR
                   generic_meta->>'budget_snapshot_sha256' !~ '^[0-9a-f]{64}$' OR
                   NOT EXISTS (SELECT 1 FROM lab.run_events event
                     WHERE event.run_id=p_run_id AND event.event_type='director.checkpoint'
                       AND event.event_json->>'phase'='director_loop_state'
                       AND event.event_json->>'payload_sha256'=generic_meta->>'prior_state_sha256'
                       AND (event.event_json->>'sequence')::integer < p_sequence) THEN
                    RAISE EXCEPTION 'bitless application lacks its exact prior and resulting state';
                END IF;
            END IF;
            IF p_key='director-holdout-run-end-unavailable-intent:1' THEN
                expected_phase := 'holdout_run_end_unavailable_intent';
                generic_meta := p_metadata->'holdout_unavailable_freeze';
                IF generic_meta IS NULL OR
                   generic_meta->>'run_id' IS DISTINCT FROM p_run_id::text OR
                   generic_meta->>'admitted_generation' IS DISTINCT FROM p_generation::text OR
                   generic_meta->>'execution_sha256' IS DISTINCT FROM p_execution_sha256 OR
                   generic_meta->>'candidate_experiment_id' IS NULL OR
                   generic_meta->>'reason' IS NULL OR
                   generic_meta->>'reason' NOT IN ('fresh_wall_budget_unavailable',
                     'intent_checkpoint_without_sql_registration',
                     'reservation_checkpoint_without_sql_intent') OR
                   NOT EXISTS (SELECT 1 FROM lab.run_events event
                     WHERE event.run_id=p_run_id AND event.event_type='director.checkpoint'
                       AND event.event_json->>'key'=generic_meta->>'prior_state_key'
                       AND event.event_json->>'phase'='director_loop_state'
                       AND event.event_json->>'payload_sha256'=generic_meta->>'prior_state_sha256'
                       AND (event.event_json->>'sequence')::integer=
                           (generic_meta->>'prior_state_sequence')::integer
                       AND (event.event_json->>'sequence')::integer < p_sequence) THEN
                    RAISE EXCEPTION 'unavailable freeze lacks its exact prior state';
                END IF;
                IF generic_meta->>'reason'='fresh_wall_budget_unavailable' AND (
                    generic_meta->>'remaining_wall_seconds' IS NULL OR
                    (generic_meta->>'remaining_wall_seconds')::double precision >= 1 OR
                    EXISTS (SELECT 1 FROM lab.run_events event
                     WHERE event.run_id=p_run_id AND event.event_type='director.checkpoint'
                       AND (event.event_json->>'key'='director-holdout-run-end-intent' OR
                            event.event_json->'holdout_reservation'->>'trigger_kind'='run_end'))
                ) THEN RAISE EXCEPTION 'fresh unavailable freeze has prior admitted work'; END IF;
                IF generic_meta->>'reason'='intent_checkpoint_without_sql_registration' AND
                   NOT EXISTS (SELECT 1 FROM lab.run_events event
                     WHERE event.run_id=p_run_id AND event.event_type='director.checkpoint'
                       AND event.event_json->>'key'='director-holdout-run-end-intent'
                       AND event.event_json->>'phase'='holdout_run_end_intent'
                       AND event.event_json->>'payload_sha256'=
                           generic_meta->>'original_intent_checkpoint_sha256'
                       AND (event.event_json->>'sequence')::integer=
                           (generic_meta->>'original_intent_checkpoint_sequence')::integer
                       AND (event.event_json->>'sequence')::integer < p_sequence) THEN
                    RAISE EXCEPTION 'unavailable freeze lacks its exact original intent';
                END IF;
                IF EXISTS (SELECT 1 FROM lab.holdout_run_end_intents WHERE run_id=p_run_id)
                   OR EXISTS (SELECT 1 FROM lab.holdout_reservations
                               WHERE run_id=p_run_id AND trigger_kind='run_end'
                                 AND trigger_index=1) OR
                   EXISTS (SELECT 1 FROM lab.holdout_run_end_fences WHERE run_id=p_run_id) OR
                   EXISTS (SELECT 1 FROM lab.holdout_run_end_unavailable WHERE run_id=p_run_id) THEN
                    RAISE EXCEPTION 'new unavailable intent is forbidden after holdout admission';
                END IF;
                freeze_meta := p_metadata->'holdout_reservation_unavailable';
                IF freeze_meta IS NOT NULL THEN
                    IF EXISTS (SELECT 1 FROM lab.run_events event
                        WHERE event.run_id=p_run_id AND event.event_type='director.checkpoint'
                          AND event.event_json->>'key'='director-holdout-run-end-intent') OR
                       freeze_meta->>'run_id' IS DISTINCT FROM p_run_id::text OR
                       freeze_meta->>'candidate_experiment_id' IS NULL OR
                       freeze_meta->>'admitted_generation' IS DISTINCT FROM p_generation::text OR
                       freeze_meta->>'execution_sha256' IS DISTINCT FROM p_execution_sha256 OR
                       freeze_meta->>'reservation_checkpoint_key' IS NULL OR
                       freeze_meta->>'reservation_checkpoint_key' !~
                           '^holdout-budget-reserved:[0-9a-f-]{36}$' OR
                       freeze_meta->>'reservation_checkpoint_sha256' IS NULL OR
                       freeze_meta->>'reservation_checkpoint_sha256' !~ '^[0-9a-f]{64}$' OR
                       freeze_meta->>'reconciliation_checkpoint_key' IS NULL OR
                       freeze_meta->>'reconciliation_checkpoint_key' !~
                           '^holdout-budget-reconciled:[0-9a-f-]{36}$' OR
                       freeze_meta->>'reconciliation_checkpoint_sha256' IS NULL OR
                       freeze_meta->>'reconciliation_checkpoint_sha256' !~ '^[0-9a-f]{64}$' OR
                       freeze_meta->>'budget_snapshot_sha256' IS NULL OR
                       freeze_meta->>'budget_snapshot_sha256' !~ '^[0-9a-f]{64}$' THEN
                        RAISE EXCEPTION 'reservation-only unavailable freeze is malformed';
                    END IF;
                    SELECT event_json INTO reservation_event FROM lab.run_events event
                     WHERE event.run_id=p_run_id AND event.event_type='director.checkpoint'
                       AND event.event_json->>'key'=freeze_meta->>'reservation_checkpoint_key';
                    reservation_meta := reservation_event->'holdout_reservation';
                    SELECT event_json INTO reconciliation_event FROM lab.run_events event
                     WHERE event.run_id=p_run_id AND event.event_type='director.checkpoint'
                       AND event.event_json->>'key'=freeze_meta->>'reconciliation_checkpoint_key';
                    reconciliation_meta := reconciliation_event->'holdout_budget_reconciliation';
                    IF reservation_event IS NULL OR reconciliation_event IS NULL OR
                       reservation_event->>'phase' IS DISTINCT FROM 'holdout_budget_reserved' OR
                       reservation_event->>'payload_sha256' IS DISTINCT FROM
                           freeze_meta->>'reservation_checkpoint_sha256' OR
                       (reservation_event->>'sequence')::integer IS DISTINCT FROM
                           (freeze_meta->>'reservation_checkpoint_sequence')::integer OR
                       reconciliation_event->>'phase'
                           IS DISTINCT FROM 'holdout_budget_reconciled' OR
                       reconciliation_event->>'payload_sha256' IS DISTINCT FROM
                           freeze_meta->>'reconciliation_checkpoint_sha256' OR
                       (reconciliation_event->>'sequence')::integer IS DISTINCT FROM
                           (freeze_meta->>'reconciliation_checkpoint_sequence')::integer OR
                       reservation_meta->>'candidate_experiment_id' IS DISTINCT FROM
                           freeze_meta->>'candidate_experiment_id' OR
                       reservation_meta->>'admitted_generation'
                           IS DISTINCT FROM p_generation::text OR
                       reservation_meta->>'execution_sha256' IS DISTINCT FROM p_execution_sha256 OR
                       reconciliation_meta->>'candidate_experiment_id' IS DISTINCT FROM
                           freeze_meta->>'candidate_experiment_id' OR
                       reconciliation_meta->>'admitted_generation'
                           IS DISTINCT FROM p_generation::text OR
                       reconciliation_meta->>'execution_sha256'
                           IS DISTINCT FROM p_execution_sha256 OR
                       reconciliation_meta->>'budget_snapshot_sha256' IS DISTINCT FROM
                           freeze_meta->>'budget_snapshot_sha256' THEN
                        RAISE EXCEPTION 'reservation-only unavailable freeze receipts differ';
                    END IF;
                END IF;
            ELSIF p_key='holdout-run-end-unavailable:1' THEN
                expected_phase := 'holdout_run_end_unavailable';
                SELECT * INTO unavailable_item FROM lab.holdout_run_end_unavailable receipt
                 WHERE receipt.run_id=p_run_id
                   AND receipt.admitted_generation=p_generation
                   AND receipt.execution_sha256=p_execution_sha256;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'unavailable application lacks its exact SQL fence';
                END IF;
                IF generic_meta->>'candidate_experiment_id' IS DISTINCT FROM
                       unavailable_item.candidate_experiment_id OR
                   generic_meta->>'reason' IS DISTINCT FROM unavailable_item.reason OR
                   generic_meta->>'prior_state_sha256' IS DISTINCT FROM
                       unavailable_item.prior_state_sha256 OR
                   generic_meta->>'budget_snapshot_sha256' IS DISTINCT FROM
                       unavailable_item.budget_snapshot_sha256 OR
                   generic_meta->>'unavailable_checkpoint_sha256' IS DISTINCT FROM
                       unavailable_item.unavailable_checkpoint_sha256 OR
                   (generic_meta->>'unavailable_checkpoint_sequence')::integer IS DISTINCT FROM
                       unavailable_item.unavailable_checkpoint_sequence THEN
                    RAISE EXCEPTION 'unavailable application differs from its immutable SQL fence';
                END IF;
                IF unavailable_item.reason='reservation_checkpoint_without_sql_intent' AND (
                    p_metadata->'holdout_reservation_unavailable_application' IS NULL OR
                    p_metadata->'holdout_reservation_unavailable_application'->>'run_id'
                       IS DISTINCT FROM p_run_id::text OR
                    p_metadata->'holdout_reservation_unavailable_application'->>'candidate_experiment_id'
                       IS DISTINCT FROM unavailable_item.candidate_experiment_id OR
                    p_metadata->'holdout_reservation_unavailable_application'->>'reservation_checkpoint_key'
                       IS DISTINCT FROM unavailable_item.reservation_checkpoint_key OR
                    p_metadata->'holdout_reservation_unavailable_application'->>'reservation_checkpoint_sha256'
                       IS DISTINCT FROM unavailable_item.reservation_checkpoint_sha256 OR
                    (p_metadata->'holdout_reservation_unavailable_application'->>'reservation_checkpoint_sequence')::integer
                       IS DISTINCT FROM unavailable_item.reservation_checkpoint_sequence OR
                    p_metadata->'holdout_reservation_unavailable_application'->>'budget_snapshot_sha256'
                       IS DISTINCT FROM unavailable_item.budget_snapshot_sha256 OR
                    (p_metadata->'holdout_reservation_unavailable_application'->>'admitted_generation')::integer
                       IS DISTINCT FROM p_generation OR
                    p_metadata->'holdout_reservation_unavailable_application'->>'execution_sha256'
                       IS DISTINCT FROM p_execution_sha256 OR
                    p_metadata->'holdout_reservation_unavailable_application'->>'state'
                       IS DISTINCT FROM 'failed' OR
                    p_metadata->'holdout_reservation_unavailable_application'->'bit'
                       IS DISTINCT FROM 'null'::jsonb) THEN
                    RAISE EXCEPTION 'reservation-only application differs from its SQL fence';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM lab.holdout_run_end_unavailable receipt
                                WHERE receipt.run_id=p_run_id
                                  AND receipt.admitted_generation=p_generation
                                  AND receipt.execution_sha256=p_execution_sha256) THEN
                    RAISE EXCEPTION 'unavailable application lacks its exact SQL fence';
                END IF;
            ELSIF p_key='holdout-run-end-admission-failure:1' THEN
                expected_phase := 'holdout_run_end_admission_failure';
                IF NOT EXISTS (SELECT 1 FROM lab.holdout_run_end_fences receipt
                                WHERE receipt.run_id=p_run_id
                                  AND receipt.admitted_generation=p_generation
                                  AND receipt.execution_sha256=p_execution_sha256
                                  AND receipt.candidate_experiment_id=
                                      generic_meta->>'candidate_experiment_id'
                                  AND receipt.intent_checkpoint_sha256=
                                      generic_meta->>'intent_checkpoint_sha256'
                                  AND receipt.intent_checkpoint_sequence=
                                      (generic_meta->>'intent_checkpoint_sequence')::integer
                                  AND receipt.budget_checkpoint_key=
                                      generic_meta->>'budget_checkpoint_key'
                                  AND receipt.budget_checkpoint_sha256=
                                      generic_meta->>'budget_checkpoint_sha256') THEN
                    RAISE EXCEPTION 'admission failure application lacks its exact SQL fence';
                END IF;
            ELSIF p_key='holdout-application:run_end:1' THEN
                expected_phase := 'holdout_application';
                app_meta := p_metadata->'holdout_application';
                IF app_meta IS NULL OR
                   app_meta->>'run_id' IS DISTINCT FROM p_run_id::text OR
                   app_meta->>'trigger_kind' IS DISTINCT FROM 'run_end' OR
                   app_meta->>'trigger_index' IS DISTINCT FROM '1' OR
                   app_meta->>'admitted_generation' IS DISTINCT FROM p_generation::text OR
                   app_meta->>'execution_sha256' IS DISTINCT FROM p_execution_sha256 OR
                   app_meta->>'state' IS DISTINCT FROM 'failed' OR
                   app_meta->'bit' IS DISTINCT FROM 'null'::jsonb OR
                   app_meta->>'reservation_id' IS NULL OR
                   app_meta->>'candidate_experiment_id' IS NULL THEN
                    RAISE EXCEPTION 'run-end bitless application metadata is malformed';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM lab.run_events event
                    WHERE event.run_id=p_run_id AND event.event_type='director.checkpoint'
                      AND event.event_json->>'key'=generic_meta->>'budget_checkpoint_key'
                      AND event.event_json->>'phase'='holdout_budget_reconciled'
                      AND event.event_json->>'payload_sha256'=
                          generic_meta->>'budget_checkpoint_sha256'
                      AND event.event_json->'holdout_budget_reconciliation'
                          ->>'budget_snapshot_sha256'=generic_meta->>'budget_snapshot_sha256'
                      AND event.event_json->'holdout_budget_reconciliation'
                          ->>'candidate_experiment_id'=generic_meta->>'candidate_experiment_id'
                      AND event.event_json->'holdout_closure_owner'->>'admitted_generation'=
                          p_generation::text
                      AND event.event_json->'holdout_closure_owner'->>'execution_sha256'=
                          p_execution_sha256
                      AND (event.event_json->>'sequence')::integer < p_sequence) THEN
                    RAISE EXCEPTION 'failed application lacks its exact reconciled budget';
                END IF;
                SELECT * INTO reservation_item FROM lab.holdout_reservations
                 WHERE reservation_id=(app_meta->>'reservation_id')::uuid;
                IF reservation_item.reservation_id IS NULL OR
                   reservation_item.run_id IS DISTINCT FROM p_run_id OR
                   reservation_item.candidate_experiment_id IS DISTINCT FROM
                       app_meta->>'candidate_experiment_id' OR
                   reservation_item.trigger_kind IS DISTINCT FROM 'run_end' OR
                   reservation_item.trigger_index IS DISTINCT FROM 1 OR
                   reservation_item.state IS DISTINCT FROM 'failed' OR
                   reservation_item.result_bit IS NOT NULL OR
                   reservation_item.admitted_generation IS DISTINCT FROM p_generation OR
                   reservation_item.execution_sha256 IS DISTINCT FROM p_execution_sha256 THEN
                    RAISE EXCEPTION
                        'run-end bitless application differs from terminal Scorer receipt';
                END IF;
            ELSIF p_key='holdout-state-application:run_end:1' THEN
                expected_phase := 'holdout_state_application';
                state_meta := p_metadata->'holdout_closure_marker';
                IF state_meta IS NULL OR NOT EXISTS (
                    SELECT 1 FROM lab.run_events state_event JOIN lab.run_events app_event
                      ON app_event.run_id=state_event.run_id
                     WHERE state_event.run_id=p_run_id
                       AND state_event.event_type='director.checkpoint'
                       AND app_event.event_type='director.checkpoint'
                       AND state_event.event_json->>'phase'='director_loop_state'
                       AND state_event.event_json->>'payload_sha256'=state_meta->>'state_sha256'
                       AND state_event.event_json->'holdout_closure_state'->>'application_sha256'=
                           state_meta->>'application_sha256'
                       AND app_event.event_json->>'payload_sha256'=state_meta->>'application_sha256'
                       AND app_event.event_json->'holdout_closure_application'
                           ->>'expected_state_sha256'=
                           state_meta->>'state_sha256'
                       AND state_event.event_json->'holdout_closure_owner'->>'admitted_generation'=
                           p_generation::text
                       AND state_event.event_json->'holdout_closure_owner'->>'execution_sha256'=
                           p_execution_sha256
                       AND (app_event.event_json->>'sequence')::integer <
                           (state_event.event_json->>'sequence')::integer
                       AND (state_event.event_json->>'sequence')::integer < p_sequence
                ) THEN RAISE EXCEPTION 'holdout marker lacks its exact closure receipts'; END IF;
            ELSIF p_key ~ '^holdout-budget-reconciled:[0-9a-f-]{36}$' THEN
                expected_phase := 'holdout_budget_reconciled';
                reservation_key := 'holdout-budget-reserved:' ||
                    substring(p_key from '^holdout-budget-reconciled:([0-9a-f-]{36})$');
                SELECT event_json INTO reservation_event FROM lab.run_events event
                 WHERE event.run_id=p_run_id AND event.event_type='director.checkpoint'
                   AND event.event_json->>'key'=reservation_key;
                reservation_meta := reservation_event->'holdout_reservation';
                reconciliation_meta := p_metadata->'holdout_budget_reconciliation';
                IF NOT coalesce(reconciliation_meta ?& ARRAY[
                    'model_tokens_before','model_tokens_after','proposal_count_before',
                    'proposal_count_after','wall_seconds_before','wall_seconds_after',
                    'elapsed_wall_seconds_before','elapsed_wall_seconds_after',
                    'reserved_model_tokens_before','reserved_model_tokens_after',
                    'reserved_wall_seconds_before','reserved_wall_seconds_after',
                    'reservations_before','reservations_after','measured_wall_seconds'
                ],false) OR
                   jsonb_typeof(reservation_meta->'budget') IS DISTINCT FROM 'object' OR
                   reconciliation_meta->'reservations_before' IS DISTINCT FROM
                       reservation_meta->'budget'->'reservations' OR
                   reconciliation_meta->'reservations_after' IS DISTINCT FROM (
                       SELECT coalesce(jsonb_agg(item), '[]'::jsonb)
                         FROM jsonb_array_elements(reservation_meta->'budget'->'reservations') item
                        WHERE item->>'reservation_id'<>reservation_meta->>'reservation_id'
                   ) OR EXISTS (
                       SELECT 1 FROM (VALUES
                           ('wall_seconds','wall_seconds_before'),
                           ('elapsed_wall_seconds','elapsed_wall_seconds_before'),
                           ('proposal_count','proposal_count_before'),
                           ('model_tokens','model_tokens_before'),
                           ('reserved_wall_seconds','reserved_wall_seconds_before'),
                           ('reserved_model_tokens','reserved_model_tokens_before')
                       ) pair(budget_key,receipt_key)
                       WHERE reservation_meta->'budget'->pair.budget_key IS DISTINCT FROM
                           reconciliation_meta->pair.receipt_key
                   ) THEN
                    RAISE EXCEPTION 'closure reconciliation changed its durable original budget';
                END IF;
                IF reservation_event IS NULL OR
                   reservation_event->>'phase' IS DISTINCT FROM 'holdout_budget_reserved' OR
                   (reservation_event->>'sequence')::integer >= p_sequence OR
                   reservation_meta->>'reservation_id' IS DISTINCT FROM
                       substring(p_key from '^holdout-budget-reconciled:([0-9a-f-]{36})$') OR
                   reservation_meta->>'trigger_kind' IS DISTINCT FROM 'run_end' OR
                   reservation_meta->>'trigger_index' IS DISTINCT FROM '1' OR
                   reservation_meta->>'admitted_generation' IS DISTINCT FROM p_generation::text OR
                   reservation_meta->>'execution_sha256' IS DISTINCT FROM p_execution_sha256 OR
                   reconciliation_meta IS NULL OR
                   reconciliation_meta->>'reservation_id' IS DISTINCT FROM
                       substring(p_key from '^holdout-budget-reconciled:([0-9a-f-]{36})$') OR
                   reconciliation_meta->>'trigger_kind' IS DISTINCT FROM 'run_end' OR
                   reconciliation_meta->>'trigger_index' IS DISTINCT FROM '1' OR
                   reconciliation_meta->>'admitted_generation'
                       IS DISTINCT FROM p_generation::text OR
                   reconciliation_meta->>'execution_sha256' IS DISTINCT FROM p_execution_sha256 OR
                   (reconciliation_meta->>'reserved_wall_seconds')::integer IS DISTINCT FROM
                       (reservation_meta->>'wall_seconds')::integer OR
                   (reconciliation_meta->>'measured_wall_seconds')::double precision <
                       (reservation_meta->>'wall_seconds')::integer OR
                   (reconciliation_meta->>'wall_seconds_after')::double precision <
                       (reconciliation_meta->>'wall_seconds_before')::double precision +
                       (reservation_meta->>'wall_seconds')::integer OR
                   (reconciliation_meta->>'proposal_count_after')::integer IS DISTINCT FROM
                       (reconciliation_meta->>'proposal_count_before')::integer OR
                   (reconciliation_meta->>'model_tokens')::integer IS DISTINCT FROM 0 OR
                   (reconciliation_meta->>'model_tokens_before')::integer IS DISTINCT FROM
                       (reconciliation_meta->>'model_tokens_after')::integer OR
                   (reconciliation_meta->>'wall_seconds_after')::double precision
                       IS DISTINCT FROM
                       (reconciliation_meta->>'wall_seconds_before')::double precision +
                       (reconciliation_meta->>'measured_wall_seconds')::double precision OR
                   (reconciliation_meta->>'elapsed_wall_seconds_after')::double precision <
                       (reconciliation_meta->>'elapsed_wall_seconds_before')::double precision OR
                   (reconciliation_meta->>'reserved_model_tokens_after')::integer IS DISTINCT FROM
                       (reconciliation_meta->>'reserved_model_tokens_before')::integer OR
                   (reconciliation_meta->>'reserved_wall_seconds_after')::double precision
                       IS DISTINCT FROM
                       (reconciliation_meta->>'reserved_wall_seconds_before')::double precision -
                       (reservation_meta->>'wall_seconds')::integer OR
                   NOT (reconciliation_meta->'reservation_ids_before' @>
                       jsonb_build_array(reservation_meta->>'reservation_id')) OR
                   reconciliation_meta->'reservation_ids_after' @>
                       jsonb_build_array(reservation_meta->>'reservation_id') OR
                   jsonb_array_length(reconciliation_meta->'reservation_ids_before') < 1 OR
                   jsonb_array_length(reconciliation_meta->'reservation_ids_before') <>
                       jsonb_array_length(reconciliation_meta->'reservation_ids_after')+1 OR
                   EXISTS (SELECT 1 FROM jsonb_array_elements_text(
                       reconciliation_meta->'reservation_ids_before') AS item(id)
                       WHERE item.id<>reservation_meta->>'reservation_id' AND NOT
                           reconciliation_meta->'reservation_ids_after' @>
                               jsonb_build_array(item.id)) OR
                   NOT EXISTS (SELECT 1 FROM lab.holdout_run_end_intents intent
                       WHERE intent.run_id=p_run_id
                         AND intent.admitted_generation=p_generation
                         AND intent.execution_sha256=p_execution_sha256) AND
                   (EXISTS (SELECT 1 FROM lab.holdout_reservations reservation
                       WHERE reservation.run_id=p_run_id AND reservation.trigger_kind='run_end') OR
                    (SELECT state FROM lab.runs WHERE run_id=p_run_id)='running' AND
                    (SELECT deadline_at FROM lab.director_execution_contracts
                      WHERE run_id=p_run_id)>clock_timestamp() AND
                    NOT (SELECT stop_requested FROM lab.runs WHERE run_id=p_run_id)) THEN
                    RAISE EXCEPTION
                        'run-end budget reconciliation lacks its exact reservation closure';
                END IF;
            ELSIF p_key ~ '^director-state[:][0-9]+([:]holdout[:][0-9]+)?[:]run_end$' THEN
                expected_phase := 'director_loop_state';
                state_meta := p_metadata->'holdout_closure_state';
                IF state_meta IS NULL OR
                   state_meta->>'state_sha256' IS DISTINCT FROM p_payload_sha256 OR
                   NOT EXISTS (SELECT 1 FROM lab.run_events event
                    WHERE event.run_id=p_run_id AND event.event_type='director.checkpoint'
                      AND event.event_json->>'payload_sha256'=state_meta->>'application_sha256'
                      AND event.event_json->'holdout_closure_owner'->>'admitted_generation'=
                          p_generation::text
                      AND event.event_json->'holdout_closure_owner'->>'execution_sha256'=
                          p_execution_sha256
                      AND event.event_json->'holdout_closure_application'->>'expected_state_sha256'=
                          p_payload_sha256
                      AND (event.event_json->>'sequence')::integer < p_sequence) THEN
                    RAISE EXCEPTION 'run-end closure state differs from its bitless application';
                END IF;
            ELSE
                RAISE EXCEPTION 'checkpoint key is outside the holdout closure allowlist';
            END IF;
            IF p_phase IS DISTINCT FROM expected_phase THEN
                RAISE EXCEPTION 'checkpoint phase differs from the holdout closure allowlist';
            END IF;
            END IF; -- immutable retry or new closure validation
            PERFORM set_config('lab.holdout_checkpoint_closure_run_id',p_run_id::text,true);
            PERFORM set_config('lab.holdout_checkpoint_closure_generation',p_generation::text,true);
            PERFORM set_config('lab.holdout_checkpoint_closure_execution_sha256',
                               p_execution_sha256,true);
            PERFORM set_config('lab.holdout_checkpoint_closure_key',p_key,true);
            PERFORM set_config('lab.holdout_checkpoint_closure_phase',p_phase,true);
            PERFORM set_config('lab.holdout_checkpoint_closure_sha256',p_payload_sha256,true);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION lab.guard_director_checkpoint_insert()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        BEGIN
            IF NEW.event_type='director.checkpoint' THEN
                IF nullif(current_setting(
                    'lab.holdout_checkpoint_closure_run_id',true),'') IS NOT NULL THEN
                    IF current_setting('lab.holdout_checkpoint_closure_run_id',true)
                           IS DISTINCT FROM NEW.run_id::text OR
                       current_setting('lab.holdout_checkpoint_closure_key',true)
                           IS DISTINCT FROM NEW.event_json->>'key' OR
                       current_setting('lab.holdout_checkpoint_closure_phase',true)
                           IS DISTINCT FROM NEW.event_json->>'phase' OR
                       current_setting('lab.holdout_checkpoint_closure_sha256',true)
                           IS DISTINCT FROM NEW.event_json->>'payload_sha256' THEN
                        RAISE EXCEPTION 'holdout closure checkpoint differs from its authorization';
                    END IF;
                    PERFORM lab.assert_director_holdout_checkpoint(
                        NEW.run_id,
                        nullif(current_setting(
                            'lab.holdout_checkpoint_closure_generation',true),'')::integer,
                        nullif(current_setting('lab.owner_invocation_id',true),''),
                        nullif(current_setting(
                        'lab.holdout_checkpoint_closure_execution_sha256',true),''),
                        NEW.event_json->>'key',NEW.event_json->>'phase',
                        (NEW.event_json->>'sequence')::integer,
                        NEW.event_json->>'payload_sha256',NEW.event_json
                    );
                ELSE
                    PERFORM lab.assert_director_owner_context(NEW.run_id);
                    IF NEW.event_json->>'phase'='holdout_budget_reserved' AND
                       NEW.event_json->>'key' ~ '^holdout-budget-reserved:[0-9a-f-]{36}$' AND
                       NEW.event_json->'holdout_reservation' IS NOT NULL THEN
                        IF NEW.event_json->'holdout_reservation' IS NULL OR
                           NEW.event_json->'holdout_reservation'->>'reservation_id' IS DISTINCT FROM
                             substring(NEW.event_json->>'key' from
                               '^holdout-budget-reserved:([0-9a-f-]{36})$') OR
                           NEW.event_json->'holdout_reservation'->>'trigger_kind'
                             IS DISTINCT FROM 'run_end' OR
                           NEW.event_json->'holdout_reservation'->>'trigger_index'
                             IS DISTINCT FROM '1' OR
                           NEW.event_json->'holdout_reservation'->>'admitted_generation'
                             IS DISTINCT FROM current_setting('lab.owner_generation',true) OR
                           NEW.event_json->'holdout_reservation'->>'execution_sha256'
                            
                                 IS DISTINCT FROM
                                 current_setting('lab.owner_execution_sha256',true) THEN
                            RAISE EXCEPTION
                                'run-end reservation checkpoint lacks captured owner metadata';
                        END IF;
                    END IF;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.assert_scorer_holdout_execution(
            p_reservation_id uuid,p_generation integer,p_execution_sha256 text,p_operation text
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE target_run uuid; run_item lab.runs%ROWTYPE;
            control_item lab.director_execution_control%ROWTYPE;
            contract_item lab.director_execution_contracts%ROWTYPE;
            item lab.holdout_reservations%ROWTYPE; lock_key bigint;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' OR p_reservation_id IS NULL OR
               p_generation IS NULL OR p_generation < 1 OR
               p_execution_sha256 IS NULL OR p_execution_sha256 !~ '^[0-9a-f]{64}$' OR
               p_operation IS NULL OR p_operation NOT IN ('active','closure') THEN
                RAISE EXCEPTION 'Scorer holdout owner identity is malformed';
            END IF;
            SELECT run_id INTO target_run FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout reservation is unavailable'; END IF;
            lock_key := ('x' || substr(encode(sha256(uuid_send(target_run)), 'hex'), 1, 16))
                ::bit(64)::bigint;
            PERFORM pg_advisory_xact_lock(lock_key);
            SELECT * INTO run_item FROM lab.runs WHERE run_id=target_run FOR UPDATE;
            SELECT * INTO control_item FROM lab.director_execution_control
             WHERE run_id=target_run FOR UPDATE;
            SELECT * INTO contract_item FROM lab.director_execution_contracts
             WHERE run_id=target_run;
            SELECT * INTO item FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id FOR UPDATE;
            IF run_item.run_id IS NULL OR control_item.run_id IS NULL OR
               contract_item.run_id IS NULL OR item.reservation_id IS NULL OR
               item.run_id IS DISTINCT FROM target_run OR
               item.admitted_generation IS DISTINCT FROM p_generation OR
               item.execution_sha256 IS DISTINCT FROM p_execution_sha256 OR
               control_item.mode <> 'active' OR
               control_item.current_generation IS DISTINCT FROM p_generation OR
               contract_item.execution_sha256 IS DISTINCT FROM p_execution_sha256 OR
               NOT EXISTS (
                   SELECT 1 FROM lab.director_owner_generations owner
                    WHERE owner.run_id=target_run AND owner.generation=p_generation
                      AND owner.execution_sha256=p_execution_sha256
               ) THEN
                RAISE EXCEPTION 'holdout reservation owner is stale or malformed';
            END IF;
            IF p_operation='active' AND (
               run_item.state <> 'running' OR run_item.stop_requested OR
               contract_item.deadline_at <= clock_timestamp()
            ) THEN
                RAISE EXCEPTION 'holdout execution is no longer active';
            END IF;
            IF p_operation='closure' AND run_item.state NOT IN
               ('running','stop_requested','stopped','failed') THEN
                RAISE EXCEPTION 'holdout closure is unavailable for this run state';
            END IF;
            PERFORM set_config('lab.scorer_holdout_run_id',target_run::text,true);
            PERFORM set_config('lab.scorer_holdout_generation',p_generation::text,true);
            PERFORM set_config('lab.scorer_holdout_execution_sha256',p_execution_sha256,true);
            PERFORM set_config('lab.scorer_holdout_operation',p_operation,true);
        END;
        $$
        """
    )
    # These triggers copy only a pair proven by the ordered assertion above.
    op.execute(
        """
        CREATE FUNCTION lab.guard_holdout_owner_pair()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE owner_run uuid; owner_generation integer; owner_digest text;
        BEGIN
            IF TG_OP='UPDATE' THEN
                IF ROW(OLD.admitted_generation,OLD.execution_sha256)
                   IS DISTINCT FROM ROW(NEW.admitted_generation,NEW.execution_sha256) THEN
                    RAISE EXCEPTION 'holdout admitted owner identity is immutable';
                END IF;
            END IF;
            IF TG_OP='INSERT' THEN
                IF session_user <> 'swapp_lab_director' THEN
                    RAISE EXCEPTION 'new holdout admission requires Director';
                END IF;
                BEGIN
                    owner_run := nullif(current_setting('lab.holdout_run_id',true),'')::uuid;
                    owner_generation := nullif(
                        current_setting('lab.holdout_generation',true),'')::integer;
                EXCEPTION WHEN OTHERS THEN
                    RAISE EXCEPTION 'Director holdout owner context is malformed';
                END;
                owner_digest := nullif(
                    current_setting('lab.holdout_execution_sha256',true),'');
                IF NEW.admitted_generation IS NULL AND NEW.execution_sha256 IS NULL THEN
                    NEW.admitted_generation := owner_generation;
                    NEW.execution_sha256 := owner_digest;
                END IF;
                IF owner_run IS NULL OR owner_run IS DISTINCT FROM NEW.run_id OR
                   owner_generation IS NULL OR owner_digest IS NULL OR
                   owner_generation IS DISTINCT FROM NEW.admitted_generation OR
                   owner_digest IS DISTINCT FROM NEW.execution_sha256 THEN
                    RAISE EXCEPTION 'holdout admission has no matching Director owner';
                END IF;
            ELSE
                IF session_user <> 'swapp_lab_scorer' THEN
                    RAISE EXCEPTION 'holdout result mutation requires Scorer';
                END IF;
                BEGIN
                    owner_run := nullif(
                        current_setting('lab.scorer_holdout_run_id',true),'')::uuid;
                    owner_generation := nullif(
                        current_setting('lab.scorer_holdout_generation',true),'')::integer;
                EXCEPTION WHEN OTHERS THEN
                    RAISE EXCEPTION 'Scorer holdout owner context is malformed';
                END;
                owner_digest := nullif(
                    current_setting('lab.scorer_holdout_execution_sha256',true),'');
                IF owner_run IS NULL OR owner_run IS DISTINCT FROM NEW.run_id OR
                   owner_generation IS DISTINCT FROM OLD.admitted_generation OR
                   owner_digest IS DISTINCT FROM OLD.execution_sha256 THEN
                    RAISE EXCEPTION 'holdout result is not bound to its admitted owner';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table, schema in _TABLES:
        if table == "holdout_reservations":
            op.execute(
                "CREATE TRIGGER holdout_owner_pair_guard BEFORE INSERT OR UPDATE ON "
                "lab.holdout_reservations FOR EACH ROW EXECUTE FUNCTION "
                "lab.guard_holdout_owner_pair()"
            )
        else:
            op.execute(
                f"CREATE TRIGGER {table}_owner_pair_guard BEFORE INSERT OR UPDATE ON "
                f"{schema}.{table} FOR EACH ROW EXECUTE FUNCTION lab.guard_holdout_owner_pair()"
            )
    # Generation-bound entry points. Legacy mutation signatures are revoked below.
    op.execute(
        """
        CREATE FUNCTION lab.reserve_holdout_check(
            p_run_id uuid,p_reservation_id uuid,p_request_key text,
            p_candidate_experiment_id text,p_trigger_kind text,p_trigger_index integer,
            p_generation integer,p_invocation_id text,p_execution_sha256 text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE value jsonb; stored_generation integer; stored_digest text;
        BEGIN
            PERFORM lab.assert_director_holdout_execution(
                p_run_id,p_generation,p_invocation_id,p_execution_sha256);
            value := lab.reserve_holdout_check(
                p_run_id,p_reservation_id,p_request_key,p_candidate_experiment_id,
                p_trigger_kind,p_trigger_index);
            SELECT admitted_generation,execution_sha256
              INTO stored_generation,stored_digest FROM lab.holdout_reservations
             WHERE run_id=p_run_id AND request_key=p_request_key;
            IF stored_generation IS DISTINCT FROM p_generation OR
               stored_digest IS DISTINCT FROM p_execution_sha256 THEN
                RAISE EXCEPTION 'holdout retry differs from its admitted owner pair';
            END IF;
            RETURN value || jsonb_build_object(
                'admitted_generation',p_generation,'execution_sha256',p_execution_sha256);
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.register_holdout_run_end_intent(
            p_run_id uuid,p_intent jsonb,p_generation integer,p_invocation_id text,
            p_execution_sha256 text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE value jsonb; stored_generation integer; stored_digest text;
        BEGIN
            PERFORM lab.assert_director_holdout_execution(
                p_run_id,p_generation,p_invocation_id,p_execution_sha256);
            IF EXISTS (SELECT 1 FROM lab.holdout_run_end_unavailable
                       WHERE run_id=p_run_id) THEN
                RAISE EXCEPTION 'run-end admission is durably unavailable';
            END IF;
            IF p_intent->>'admitted_generation' IS DISTINCT FROM p_generation::text OR
               p_intent->>'execution_sha256' IS DISTINCT FROM p_execution_sha256 THEN
                RAISE EXCEPTION 'run-end intent payload differs from its admitted owner pair';
            END IF;
            value := lab.register_holdout_run_end_intent(p_run_id,p_intent);
            SELECT admitted_generation,execution_sha256
              INTO stored_generation,stored_digest FROM lab.holdout_run_end_intents
             WHERE run_id=p_run_id;
            IF stored_generation IS DISTINCT FROM p_generation OR
               stored_digest IS DISTINCT FROM p_execution_sha256 THEN
                RAISE EXCEPTION 'run-end intent retry differs from its admitted owner pair';
            END IF;
            RETURN value || jsonb_build_object(
                'admitted_generation',p_generation,'execution_sha256',p_execution_sha256);
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.fence_missing_holdout_run_end(
            p_run_id uuid,p_intent_checkpoint_sha256 text,p_intent_checkpoint_sequence integer,
            p_budget_checkpoint_key text,p_budget_checkpoint_sha256 text,
            p_generation integer,p_invocation_id text,p_execution_sha256 text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE value jsonb; stored_generation integer; stored_digest text;
        BEGIN
            PERFORM lab.assert_director_holdout_closure(
                p_run_id,p_generation,p_invocation_id,p_execution_sha256);
            value := lab.fence_missing_holdout_run_end(
                p_run_id,p_intent_checkpoint_sha256,p_intent_checkpoint_sequence,
                p_budget_checkpoint_key,p_budget_checkpoint_sha256);
            SELECT admitted_generation,execution_sha256
              INTO stored_generation,stored_digest FROM lab.holdout_run_end_fences
             WHERE run_id=p_run_id;
            IF stored_generation IS DISTINCT FROM p_generation OR
               stored_digest IS DISTINCT FROM p_execution_sha256 THEN
                RAISE EXCEPTION 'run-end fence retry differs from its admitted owner pair';
            END IF;
            RETURN value || jsonb_build_object(
                'admitted_generation',p_generation,'execution_sha256',p_execution_sha256);
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.fence_run_end_unavailable(
            p_run_id uuid,p_candidate text,p_reason text,p_state_key text,p_state_sha text,
            p_state_seq integer,p_budget_sha text,p_checkpoint_sha text,p_checkpoint_seq integer,
            p_remaining_wall double precision,p_intent_sha text,p_intent_seq integer,
            p_generation integer,p_invocation_id text,p_execution_sha256 text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE value jsonb; stored_generation integer; stored_digest text;
            run_state text; run_item lab.runs%ROWTYPE;
            existing lab.holdout_run_end_unavailable%ROWTYPE;
            state_event jsonb; unavailable_event jsonb; intent_event jsonb;
            request_wall integer;
        BEGIN
            PERFORM lab.assert_director_holdout_closure(
                p_run_id,p_generation,p_invocation_id,p_execution_sha256);
            IF NOT EXISTS (SELECT 1 FROM lab.holdout_run_end_unavailable WHERE run_id=p_run_id)
               AND (EXISTS (SELECT 1 FROM lab.experiments WHERE run_id=p_run_id AND status IN
                    ('proposed','primary_running','awaiting_confirmation','confirmation_running'))
                 OR EXISTS (SELECT 1 FROM lab.run_events WHERE run_id=p_run_id
                    AND event_type='director.checkpoint'
                    AND event_json->>'phase'='director_loop_state'
                    AND (event_json->>'sequence')::integer > p_state_seq)) THEN
                RAISE EXCEPTION 'run-end unavailable prior state is stale or has unfinished work';
            END IF;
            SELECT state INTO run_state FROM lab.runs WHERE run_id=p_run_id;
            IF run_state='stop_requested' THEN
                SELECT * INTO run_item FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
                IF NOT FOUND OR run_item.state <> 'stop_requested' OR
                   NOT run_item.stop_requested THEN
                    RAISE EXCEPTION 'run is not awaiting stopped closure';
                END IF;
                IF p_reason IS NULL OR p_reason NOT IN (
                    'fresh_wall_budget_unavailable', 'intent_checkpoint_without_sql_registration'
                ) THEN
                    RAISE EXCEPTION 'run-end unavailable reason is invalid';
                END IF;
                IF p_candidate IS NULL OR length(p_candidate)=0 OR
                   p_state_key IS NULL OR length(p_state_key)=0 OR
                   p_state_sha IS NULL OR p_state_sha !~ '^[0-9a-f]{64}$' OR
                   p_budget_sha IS NULL OR p_budget_sha !~ '^[0-9a-f]{64}$' OR
                   p_checkpoint_sha IS NULL OR p_checkpoint_sha !~ '^[0-9a-f]{64}$' OR
                   p_state_seq IS NULL OR p_checkpoint_seq IS NULL OR
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
                IF state_event IS NULL OR state_event->>'phase' IS DISTINCT FROM
                   'director_loop_state' OR state_event->>'payload_sha256' IS DISTINCT FROM
                   p_state_sha OR
                   (state_event->>'sequence')::integer IS DISTINCT FROM p_state_seq THEN
                    RAISE EXCEPTION 'run-end unavailable prior state receipt differs';
                END IF;
                SELECT event_json INTO unavailable_event FROM lab.run_events WHERE run_id=p_run_id
                 AND event_type='director.checkpoint'
                 AND event_json->>'key'='director-holdout-run-end-unavailable-intent:1';
                IF unavailable_event IS NULL OR unavailable_event->>'phase' IS DISTINCT FROM
                   'holdout_run_end_unavailable_intent' OR
                   unavailable_event->>'payload_sha256' IS DISTINCT FROM p_checkpoint_sha OR
                   (unavailable_event->>'sequence')::integer IS DISTINCT FROM p_checkpoint_seq THEN
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
                    IF p_intent_sha !~ '^[0-9a-f]{64}$' OR p_intent_seq IS NULL OR
                       p_intent_seq < 0 OR intent_event IS NULL OR
                       intent_event->>'phase' IS DISTINCT FROM 'holdout_run_end_intent' OR
                       intent_event->>'payload_sha256' IS DISTINCT FROM p_intent_sha OR
                       (intent_event->>'sequence')::integer IS DISTINCT FROM p_intent_seq OR
                       intent_event->>'sequence' IS NULL THEN
                        RAISE EXCEPTION 'original run-end intent checkpoint is unavailable';
                    END IF;
                END IF;
                SELECT * INTO existing FROM lab.holdout_run_end_unavailable
                 WHERE run_id=p_run_id;
                IF FOUND THEN
                    IF ROW(existing.candidate_experiment_id,existing.reason,
                           existing.prior_state_key,existing.prior_state_sha256,
                           existing.prior_state_sequence,existing.budget_snapshot_sha256,
                           existing.unavailable_checkpoint_sha256,
                           existing.unavailable_checkpoint_sequence,
                           existing.original_intent_checkpoint_sha256,
                           existing.original_intent_checkpoint_sequence,
                           existing.admitted_generation,existing.execution_sha256)
                       IS DISTINCT FROM
                       ROW(p_candidate,p_reason,p_state_key,p_state_sha,p_state_seq,p_budget_sha,
                           p_checkpoint_sha,p_checkpoint_seq,p_intent_sha,p_intent_seq,
                           p_generation,p_execution_sha256) THEN
                        RAISE EXCEPTION 'run-end unavailable fence identity changed';
                    END IF;
                    value := jsonb_build_object('run_id',p_run_id,
                        'candidate_experiment_id',existing.candidate_experiment_id,
                        'reason',existing.reason,'prior_state_key',existing.prior_state_key,
                        'prior_state_sha256',existing.prior_state_sha256,
                        'prior_state_sequence',existing.prior_state_sequence,
                        'budget_snapshot_sha256',existing.budget_snapshot_sha256,
                        'unavailable_checkpoint_sha256',existing.unavailable_checkpoint_sha256,
                        'unavailable_checkpoint_sequence',existing.unavailable_checkpoint_sequence,
                        'original_intent_checkpoint_sha256',
                            existing.original_intent_checkpoint_sha256,
                        'original_intent_checkpoint_sequence',
                            existing.original_intent_checkpoint_sequence,
                        'state','failed','bit',NULL);
                ELSE
                    INSERT INTO lab.holdout_run_end_unavailable(
                        run_id,candidate_experiment_id,reason,prior_state_key,
                        prior_state_sha256,prior_state_sequence,budget_snapshot_sha256,
                        unavailable_checkpoint_sha256,unavailable_checkpoint_sequence,
                        original_intent_checkpoint_sha256,original_intent_checkpoint_sequence,
                        admitted_generation,execution_sha256
                    ) VALUES (
                        p_run_id,p_candidate,p_reason,p_state_key,p_state_sha,p_state_seq,
                        p_budget_sha,p_checkpoint_sha,p_checkpoint_seq,p_intent_sha,p_intent_seq,
                        p_generation,p_execution_sha256
                    );
                    value := jsonb_build_object('run_id',p_run_id,
                        'candidate_experiment_id',p_candidate,'reason',p_reason,
                        'prior_state_key',p_state_key,'prior_state_sha256',p_state_sha,
                        'prior_state_sequence',p_state_seq,'budget_snapshot_sha256',p_budget_sha,
                        'unavailable_checkpoint_sha256',p_checkpoint_sha,
                        'unavailable_checkpoint_sequence',p_checkpoint_seq,
                        'original_intent_checkpoint_sha256',p_intent_sha,
                        'original_intent_checkpoint_sequence',p_intent_seq,
                        'state','failed','bit',NULL);
                END IF;
            ELSE
                value := lab.fence_run_end_unavailable(
                    p_run_id,p_candidate,p_reason,p_state_key,p_state_sha,p_state_seq,p_budget_sha,
                    p_checkpoint_sha,p_checkpoint_seq,p_remaining_wall,p_intent_sha,p_intent_seq);
            END IF;
            SELECT admitted_generation,execution_sha256
              INTO stored_generation,stored_digest FROM lab.holdout_run_end_unavailable
             WHERE run_id=p_run_id;
            IF stored_generation IS DISTINCT FROM p_generation OR
               stored_digest IS DISTINCT FROM p_execution_sha256 THEN
                RAISE EXCEPTION 'run-end unavailable retry differs from its admitted owner pair';
            END IF;
            RETURN value || jsonb_build_object(
                'admitted_generation',p_generation,'execution_sha256',p_execution_sha256,
                'reservation_checkpoint_key',NULL,'reservation_checkpoint_sha256',NULL,
                'reservation_checkpoint_sequence',NULL);
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.fence_run_end_reserved_budget_without_intent(
            p_run_id uuid,p_candidate text,p_state_key text,p_state_sha text,p_state_seq integer,
            p_budget_sha text,p_checkpoint_sha text,p_checkpoint_seq integer,
            p_reservation_key text,p_reservation_sha text,p_reservation_seq integer,
            p_reconciliation_key text,p_reconciliation_sha text,p_reconciliation_seq integer,
            p_generation integer,p_invocation_id text,p_execution_sha256 text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE run_item lab.runs%ROWTYPE; contract_item lab.director_execution_contracts%ROWTYPE;
            state_event jsonb; unavailable_event jsonb; reserved_event jsonb;
            reconciliation_event jsonb; reservation_meta jsonb; reconciliation_meta jsonb;
            reservation_uuid uuid; existing lab.holdout_run_end_unavailable%ROWTYPE;
            value jsonb; after_ids jsonb; before_ids jsonb;
        BEGIN
            IF session_user <> 'swapp_lab_director' OR p_run_id IS NULL OR
               p_candidate IS NULL OR p_state_key IS NULL OR p_state_sha !~ '^[0-9a-f]{64}$' OR
               p_budget_sha !~ '^[0-9a-f]{64}$' OR p_checkpoint_sha !~ '^[0-9a-f]{64}$' OR
               p_reservation_key !~ '^holdout-budget-reserved:[0-9a-f-]{36}$' OR
               p_reservation_sha !~ '^[0-9a-f]{64}$' OR
               p_reconciliation_key !~ '^holdout-budget-reconciled:[0-9a-f-]{36}$' OR
               p_reconciliation_sha !~ '^[0-9a-f]{64}$' OR
               p_state_seq IS NULL OR p_state_seq < 0 OR p_checkpoint_seq IS NULL OR
               p_checkpoint_seq <= p_state_seq OR p_reservation_seq IS NULL OR
               p_reservation_seq < 0 OR p_reconciliation_seq IS NULL OR
               p_reconciliation_seq <= p_reservation_seq THEN
                RAISE EXCEPTION 'run-end reserved-budget closure identity is malformed';
            END IF;
            PERFORM lab.assert_director_holdout_closure(
                p_run_id,p_generation,p_invocation_id,p_execution_sha256);
            IF NOT EXISTS (SELECT 1 FROM lab.holdout_run_end_unavailable WHERE run_id=p_run_id)
               AND (EXISTS (SELECT 1 FROM lab.experiments WHERE run_id=p_run_id AND status IN
                    ('proposed','primary_running','awaiting_confirmation','confirmation_running'))
                 OR EXISTS (SELECT 1 FROM lab.run_events WHERE run_id=p_run_id
                    AND event_type='director.checkpoint'
                    AND event_json->>'phase'='director_loop_state'
                    AND (event_json->>'sequence')::integer > p_state_seq)) THEN
                RAISE EXCEPTION 'run-end unavailable prior state is stale or has unfinished work';
            END IF;
            SELECT * INTO run_item FROM lab.runs WHERE run_id=p_run_id;
            SELECT * INTO contract_item FROM lab.director_execution_contracts
             WHERE run_id=p_run_id;
            IF NOT ((run_item.state='stop_requested' AND run_item.stop_requested) OR
                    (run_item.state='running' AND contract_item.deadline_at<=clock_timestamp()) OR
                    run_item.state='stopped') THEN
                RAISE EXCEPTION 'run-end reserved-budget closure requires stop or expired deadline';
            END IF;
            IF EXISTS (SELECT 1 FROM lab.holdout_run_end_intents WHERE run_id=p_run_id) OR
               EXISTS (SELECT 1 FROM lab.holdout_reservations WHERE run_id=p_run_id
                         AND trigger_kind='run_end' AND trigger_index=1) OR
               EXISTS (SELECT 1 FROM lab.holdout_run_end_fences WHERE run_id=p_run_id) OR
               EXISTS (SELECT 1 FROM lab.run_events WHERE run_id=p_run_id
                         AND event_type='director.checkpoint'
                         AND event_json->>'key'='director-holdout-run-end-intent') THEN
                RAISE EXCEPTION 'reservation-only closure has later SQL or intent admission';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM lab.experiments WHERE run_id=p_run_id
                            AND experiment_id=p_candidate AND status='scored') THEN
                RAISE EXCEPTION 'run-end unavailable candidate is not a scored run candidate';
            END IF;
            SELECT event_json INTO state_event FROM lab.run_events WHERE run_id=p_run_id
             AND event_type='director.checkpoint' AND event_json->>'key'=p_state_key;
            SELECT event_json INTO unavailable_event FROM lab.run_events WHERE run_id=p_run_id
             AND event_type='director.checkpoint'
             AND event_json->>'key'='director-holdout-run-end-unavailable-intent:1';
            SELECT event_json INTO reserved_event FROM lab.run_events WHERE run_id=p_run_id
             AND event_type='director.checkpoint' AND event_json->>'key'=p_reservation_key;
            SELECT event_json INTO reconciliation_event FROM lab.run_events WHERE run_id=p_run_id
             AND event_type='director.checkpoint' AND event_json->>'key'=p_reconciliation_key;
            reservation_uuid := substring(
                p_reservation_key from '^holdout-budget-reserved:([0-9a-f-]{36})$')::uuid;
            reservation_meta := reserved_event->'holdout_reservation';
            reconciliation_meta := reconciliation_event->'holdout_budget_reconciliation';
            before_ids := reconciliation_meta->'reservation_ids_before';
            after_ids := reconciliation_meta->'reservation_ids_after';
            IF state_event IS NULL OR state_event->>'phase' IS DISTINCT FROM
                   'director_loop_state' OR state_event->>'payload_sha256' IS DISTINCT FROM
                   p_state_sha OR (state_event->>'sequence')::integer
                       IS DISTINCT FROM p_state_seq OR
               unavailable_event IS NULL OR unavailable_event->>'phase' IS DISTINCT FROM
                   'holdout_run_end_unavailable_intent' OR
                   unavailable_event->>'payload_sha256' IS DISTINCT FROM p_checkpoint_sha OR
                   (unavailable_event->>'sequence')::integer IS DISTINCT FROM p_checkpoint_seq OR
               reserved_event IS NULL OR reserved_event->>'phase' IS DISTINCT FROM
                   'holdout_budget_reserved' OR reserved_event->>'payload_sha256' IS DISTINCT FROM
                   p_reservation_sha OR (reserved_event->>'sequence')::integer IS DISTINCT FROM
                   p_reservation_seq OR
               p_reservation_seq >= p_reconciliation_seq OR
               reconciliation_event IS NULL OR reconciliation_event->>'phase' IS DISTINCT FROM
                   'holdout_budget_reconciled' OR
                   reconciliation_event->>'payload_sha256' IS DISTINCT FROM p_reconciliation_sha OR
                   (reconciliation_event->>'sequence')::integer IS DISTINCT FROM
                   p_reconciliation_seq OR p_reconciliation_seq >= p_checkpoint_seq OR
               reservation_meta->>'reservation_id' IS DISTINCT FROM reservation_uuid::text OR
               reservation_meta->>'trigger_kind' IS DISTINCT FROM 'run_end' OR
               reservation_meta->>'trigger_index' IS DISTINCT FROM '1' OR
               reservation_meta->>'candidate_experiment_id' IS DISTINCT FROM p_candidate OR
               reservation_meta->>'model_tokens' IS DISTINCT FROM '0' OR
               (reservation_meta->>'wall_seconds')::integer IS NULL OR
               (reservation_meta->>'wall_seconds')::integer < 1 OR
               (reservation_meta->>'admitted_generation')::integer IS DISTINCT FROM p_generation OR
               reservation_meta->>'execution_sha256' IS DISTINCT FROM p_execution_sha256 OR
               reconciliation_meta->>'reservation_id' IS DISTINCT FROM reservation_uuid::text OR
               reconciliation_meta->>'trigger_kind' IS DISTINCT FROM 'run_end' OR
               reconciliation_meta->>'trigger_index' IS DISTINCT FROM '1' OR
               reconciliation_meta->>'candidate_experiment_id' IS DISTINCT FROM p_candidate OR
               (reconciliation_meta->>'admitted_generation')::integer
                   IS DISTINCT FROM p_generation OR
               reconciliation_meta->>'execution_sha256' IS DISTINCT FROM p_execution_sha256 OR
               reconciliation_meta->>'budget_snapshot_sha256' IS DISTINCT FROM p_budget_sha OR
               (reconciliation_meta->>'reserved_wall_seconds')::integer IS DISTINCT FROM
                   (reservation_meta->>'wall_seconds')::integer OR
               (reconciliation_meta->>'measured_wall_seconds')::double precision <
                   (reservation_meta->>'wall_seconds')::integer OR
               (reconciliation_meta->>'wall_seconds_after')::double precision <
                   (reconciliation_meta->>'wall_seconds_before')::double precision +
                   (reservation_meta->>'wall_seconds')::integer OR
               (reconciliation_meta->>'proposal_count_after')::integer IS DISTINCT FROM
                   (reconciliation_meta->>'proposal_count_before')::integer OR
               (reconciliation_meta->>'model_tokens')::integer IS DISTINCT FROM 0 OR
               (reconciliation_meta->>'reserved_model_tokens_before')::integer IS DISTINCT FROM
                   (reconciliation_meta->>'reserved_model_tokens_after')::integer OR
               (reconciliation_meta->>'reserved_wall_seconds_after')::double precision
                       IS DISTINCT FROM
                   (reconciliation_meta->>'reserved_wall_seconds_before')::double precision -
                   (reservation_meta->>'wall_seconds')::integer OR
               jsonb_typeof(before_ids) IS DISTINCT FROM 'array' OR
               jsonb_typeof(after_ids) IS DISTINCT FROM 'array' OR
               NOT before_ids @> jsonb_build_array(reservation_uuid::text) OR
               after_ids @> jsonb_build_array(reservation_uuid::text) OR
               jsonb_array_length(before_ids) <> jsonb_array_length(after_ids)+1 OR
               EXISTS (SELECT 1 FROM jsonb_array_elements_text(before_ids) AS item(id)
                        WHERE item.id<>reservation_uuid::text AND
                              NOT after_ids @> jsonb_build_array(item.id)) THEN
                RAISE EXCEPTION 'run-end reserved-budget closure receipts differ';
            END IF;
            SELECT * INTO existing FROM lab.holdout_run_end_unavailable WHERE run_id=p_run_id;
            IF FOUND THEN
                IF ROW(existing.candidate_experiment_id,existing.reason,existing.prior_state_key,
                       existing.prior_state_sha256,existing.prior_state_sequence,
                       existing.budget_snapshot_sha256,existing.unavailable_checkpoint_sha256,
                       existing.unavailable_checkpoint_sequence,
                       existing.reservation_checkpoint_key,
                       existing.reservation_checkpoint_sha256,
                       existing.reservation_checkpoint_sequence,existing.admitted_generation,
                       existing.execution_sha256) IS DISTINCT FROM
                   ROW(p_candidate,'reservation_checkpoint_without_sql_intent',p_state_key,
                       p_state_sha,p_state_seq,p_budget_sha,p_checkpoint_sha,p_checkpoint_seq,
                       p_reservation_key,p_reservation_sha,p_reservation_seq,p_generation,
                       p_execution_sha256) THEN
                    RAISE EXCEPTION 'run-end reserved-budget closure identity changed';
                END IF;
            ELSE
                INSERT INTO lab.holdout_run_end_unavailable(
                    run_id,candidate_experiment_id,reason,prior_state_key,prior_state_sha256,
                    prior_state_sequence,budget_snapshot_sha256,unavailable_checkpoint_sha256,
                    unavailable_checkpoint_sequence,original_intent_checkpoint_sha256,
                    original_intent_checkpoint_sequence,reservation_checkpoint_key,
                    reservation_checkpoint_sha256,reservation_checkpoint_sequence,
                    admitted_generation,execution_sha256
                ) VALUES (
                    p_run_id,p_candidate,'reservation_checkpoint_without_sql_intent',p_state_key,
                    p_state_sha,p_state_seq,p_budget_sha,p_checkpoint_sha,p_checkpoint_seq,
                    NULL,NULL,p_reservation_key,p_reservation_sha,p_reservation_seq,p_generation,
                    p_execution_sha256
                );
            END IF;
            RETURN jsonb_build_object('run_id',p_run_id,
                'candidate_experiment_id',p_candidate,
                'reason','reservation_checkpoint_without_sql_intent',
                'prior_state_key',p_state_key,'prior_state_sha256',p_state_sha,
                'prior_state_sequence',p_state_seq,'budget_snapshot_sha256',p_budget_sha,
                'unavailable_checkpoint_sha256',p_checkpoint_sha,
                'unavailable_checkpoint_sequence',p_checkpoint_seq,
                'original_intent_checkpoint_sha256',NULL,
                'original_intent_checkpoint_sequence',NULL,
                'reservation_checkpoint_key',p_reservation_key,
                'reservation_checkpoint_sha256',p_reservation_sha,
                'reservation_checkpoint_sequence',p_reservation_seq,
                'state','failed','bit',NULL,'admitted_generation',p_generation,
                'execution_sha256',p_execution_sha256);
        END $$
        """
    )
    # Scorer writes receive the stored admitted pair and validate it before any
    # legacy implementation takes a run or reservation row lock.
    op.execute(
        """
        CREATE FUNCTION lab.claim_holdout_reservation(
            p_reservation_id uuid,p_worker_pid integer,p_start_ticks bigint,p_boot_id text,
            p_unit text,p_invocation_id text,p_cgroup text,p_generation integer,
            p_execution_sha256 text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        BEGIN
            PERFORM lab.assert_scorer_holdout_execution(
                p_reservation_id,p_generation,p_execution_sha256,'active');
            RETURN lab.claim_holdout_reservation(
                p_reservation_id,p_worker_pid,p_start_ticks,p_boot_id,p_unit,p_invocation_id,p_cgroup)
                || jsonb_build_object('admitted_generation',p_generation,
                                      'execution_sha256',p_execution_sha256);
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.check_holdout_admission(
            p_reservation_id uuid,p_run_id uuid,p_worker_pid integer,p_start_ticks bigint,
            p_boot_id text,p_unit text,p_invocation_id text,p_cgroup text,
            p_generation integer,p_execution_sha256 text
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        BEGIN
            PERFORM lab.assert_scorer_holdout_execution(
                p_reservation_id,p_generation,p_execution_sha256,'active');
            IF current_setting('lab.scorer_holdout_run_id',true) IS DISTINCT FROM p_run_id::text
                THEN RETURN false; END IF;
            RETURN lab.check_holdout_admission(p_reservation_id,p_run_id,p_worker_pid,
                p_start_ticks,p_boot_id,p_unit,p_invocation_id,p_cgroup);
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.publish_holdout_result(
            p_reservation_id uuid,p_delta double precision,p_result_json jsonb,
            p_result_sha256 text,p_worker_pid integer,p_start_ticks bigint,p_boot_id text,
            p_unit text,p_invocation_id text,p_cgroup text,p_generation integer,
            p_execution_sha256 text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        BEGIN
            PERFORM lab.assert_scorer_holdout_execution(
                p_reservation_id,p_generation,p_execution_sha256,'active');
            RETURN lab.publish_holdout_result(p_reservation_id,p_delta,p_result_json,
                p_result_sha256,p_worker_pid,p_start_ticks,p_boot_id,p_unit,p_invocation_id,p_cgroup);
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.fail_holdout_reservation(
            p_reservation_id uuid,p_error_code text,p_worker_pid integer,p_start_ticks bigint,
            p_boot_id text,p_unit text,p_invocation_id text,p_cgroup text,
            p_generation integer,p_execution_sha256 text,p_mode text
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        BEGIN
            PERFORM lab.assert_scorer_holdout_execution(
                p_reservation_id,p_generation,p_execution_sha256,p_mode);
            PERFORM lab.fail_holdout_reservation(p_reservation_id,p_error_code,p_worker_pid,
                p_start_ticks,p_boot_id,p_unit,p_invocation_id,p_cgroup);
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.recover_holdout_failure(
            p_reservation_id uuid,p_run_id uuid,p_worker_pid integer,p_start_ticks bigint,
            p_boot_id text,p_unit text,p_invocation_id text,p_cgroup text,
            p_generation integer,p_execution_sha256 text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        BEGIN
            PERFORM lab.assert_scorer_holdout_execution(
                p_reservation_id,p_generation,p_execution_sha256,'closure');
            IF current_setting('lab.scorer_holdout_run_id',true) IS DISTINCT FROM p_run_id::text
                THEN RAISE EXCEPTION 'holdout reservation belongs to another run'; END IF;
            RETURN lab.recover_holdout_failure(p_reservation_id,p_run_id,p_worker_pid,
                p_start_ticks,p_boot_id,p_unit,p_invocation_id,p_cgroup)
                || jsonb_build_object('admitted_generation',p_generation,
                                      'execution_sha256',p_execution_sha256);
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.recover_unclaimed_holdout_failure(
            p_reservation_id uuid,p_run_id uuid,p_generation integer,p_execution_sha256 text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        BEGIN
            PERFORM lab.assert_scorer_holdout_execution(
                p_reservation_id,p_generation,p_execution_sha256,'closure');
            IF current_setting('lab.scorer_holdout_run_id',true) IS DISTINCT FROM p_run_id::text
                THEN RAISE EXCEPTION 'holdout reservation belongs to another run'; END IF;
            RETURN lab.recover_unclaimed_holdout_failure(p_reservation_id,p_run_id)
                || jsonb_build_object('admitted_generation',p_generation,
                                      'execution_sha256',p_execution_sha256);
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.read_holdout_recovery_target_v2(p_reservation_id uuid)
        RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE item lab.holdout_reservations%ROWTYPE; run_item lab.runs%ROWTYPE;
            control_item lab.director_execution_control%ROWTYPE;
            target_run uuid; lock_key bigint;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'Scorer role required';
            END IF;
            SELECT run_id INTO target_run FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout reservation is unavailable'; END IF;
            lock_key := ('x' || substr(encode(sha256(uuid_send(target_run)), 'hex'), 1, 16))
                ::bit(64)::bigint;
            PERFORM pg_advisory_xact_lock(lock_key);
            SELECT * INTO run_item FROM lab.runs WHERE run_id=target_run FOR UPDATE;
            SELECT * INTO control_item FROM lab.director_execution_control
             WHERE run_id=target_run FOR UPDATE;
            SELECT * INTO item FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id FOR UPDATE;
            IF item.run_id IS DISTINCT FROM target_run THEN
                RAISE EXCEPTION 'holdout reservation identity changed';
            END IF;
            RETURN jsonb_build_object(
                'reservation_id',item.reservation_id,'run_id',item.run_id,
                'run_state',run_item.state,'stop_requested',run_item.stop_requested,
                'state',item.state,'worker_pid',item.worker_pid,
                'worker_start_ticks',item.worker_start_ticks,'worker_boot_id',item.worker_boot_id,
                'worker_unit',item.worker_unit,'worker_invocation_id',item.worker_invocation_id,
                'worker_cgroup',item.worker_cgroup,'admitted_generation',item.admitted_generation,
                'execution_sha256',item.execution_sha256);
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.list_holdout_recovery_targets_v2(p_run_id uuid)
        RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE run_item lab.runs%ROWTYPE; control_item lab.director_execution_control%ROWTYPE;
            target_row record; targets jsonb := '[]'::jsonb; lock_key bigint;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'Scorer role required';
            END IF;
            lock_key := ('x' || substr(encode(sha256(uuid_send(p_run_id)), 'hex'), 1, 16))
                ::bit(64)::bigint;
            PERFORM pg_advisory_xact_lock(lock_key);
            SELECT * INTO run_item FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout run is unavailable'; END IF;
            SELECT * INTO control_item FROM lab.director_execution_control
             WHERE run_id=p_run_id FOR UPDATE;
            IF control_item.run_id IS NULL THEN
                RAISE EXCEPTION 'holdout owner is unavailable';
            END IF;
            FOR target_row IN
                SELECT reservation_id,state,admitted_generation,execution_sha256
                  FROM lab.holdout_reservations
                 WHERE run_id=p_run_id AND state IN ('reserved','running')
                 ORDER BY created_at,reservation_id
            LOOP
                targets := targets || jsonb_build_array(jsonb_build_object(
                    'reservation_id',target_row.reservation_id,'state',target_row.state,
                    'admitted_generation',target_row.admitted_generation,
                    'execution_sha256',target_row.execution_sha256));
            END LOOP;
            RETURN targets;
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.read_holdout_bit_v2(p_run_id uuid,p_reservation_id uuid)
        RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE item lab.holdout_reservations%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'Director role required';
            END IF;
            SELECT * INTO item FROM lab.holdout_reservations
             WHERE run_id=p_run_id AND reservation_id=p_reservation_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout receipt is unavailable'; END IF;
            RETURN jsonb_build_object('reservation_id',item.reservation_id,'run_id',item.run_id,
                'candidate_experiment_id',item.candidate_experiment_id,
                'trigger_kind',item.trigger_kind,'trigger_index',item.trigger_index,
                'state',item.state,
                'bit',CASE WHEN item.state IN ('passed','reverted')
                    THEN item.result_bit ELSE NULL END,
                'admitted_generation',item.admitted_generation,
                'execution_sha256',item.execution_sha256);
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.read_holdout_request_v2(
            p_run_id uuid,p_request_key text,p_candidate_experiment_id text,
            p_trigger_kind text,p_trigger_index integer
        ) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE item lab.holdout_reservations%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'Director role required';
            END IF;
            IF p_request_key IS NULL OR length(p_request_key) NOT BETWEEN 1 AND 128 OR
               p_trigger_kind NOT IN ('keep_interval','run_end') OR p_trigger_index < 1 THEN
                RAISE EXCEPTION 'holdout request identity is invalid';
            END IF;
            SELECT * INTO item FROM lab.holdout_reservations
             WHERE run_id=p_run_id AND request_key=p_request_key;
            IF NOT FOUND THEN RETURN NULL; END IF;
            IF item.candidate_experiment_id IS DISTINCT FROM p_candidate_experiment_id OR
               item.trigger_kind IS DISTINCT FROM p_trigger_kind OR
               item.trigger_index IS DISTINCT FROM p_trigger_index THEN
                RAISE EXCEPTION 'holdout request key is bound to another candidate or trigger';
            END IF;
            RETURN jsonb_build_object('run_id',item.run_id,
                'reservation_id',item.reservation_id,
                'candidate_experiment_id',item.candidate_experiment_id,
                'trigger_kind',item.trigger_kind,'trigger_index',item.trigger_index,
                'state',item.state,
                'bit',CASE WHEN item.state IN ('passed','reverted')
                    THEN item.result_bit ELSE NULL END,
                'admitted_generation',item.admitted_generation,
                'execution_sha256',item.execution_sha256);
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.read_holdout_run_end_fence_v2(p_run_id uuid)
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
                'state','failed','bit',NULL,'failure_kind',item.failure_kind,
                'admitted_generation',item.admitted_generation,
                'execution_sha256',item.execution_sha256);
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.read_run_end_unavailable_v2(p_run_id uuid)
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
                'reservation_checkpoint_key',item.reservation_checkpoint_key,
                'reservation_checkpoint_sha256',item.reservation_checkpoint_sha256,
                'reservation_checkpoint_sequence',item.reservation_checkpoint_sequence,
                'state','failed','bit',NULL,'admitted_generation',item.admitted_generation,
                'execution_sha256',item.execution_sha256);
        END $$
        """
    )
    # Prevent using any old, ownerless mutation entry point.
    for signature, role in (
        ("lab.reserve_holdout_check(uuid,uuid,text,text,text,integer)", "swapp_lab_director"),
        ("lab.register_holdout_run_end_intent(uuid,jsonb)", "swapp_lab_director"),
        ("lab.fence_missing_holdout_run_end(uuid,text,integer,text,text)", "swapp_lab_director"),
        (
            "lab.fence_run_end_unavailable(uuid,text,text,text,text,integer,text,text,integer,"
            "double precision,text,integer)",
            "swapp_lab_director",
        ),
        (
            "lab.claim_holdout_reservation(uuid,integer,bigint,text,text,text,text)",
            "swapp_lab_scorer",
        ),
        (
            "lab.check_holdout_admission(uuid,uuid,integer,bigint,text,text,text,text)",
            "swapp_lab_scorer",
        ),
        (
            "lab.publish_holdout_result(uuid,double precision,jsonb,text,integer,bigint,text,"
            "text,text,text)",
            "swapp_lab_scorer",
        ),
        (
            "lab.fail_holdout_reservation(uuid,text,integer,bigint,text,text,text,text)",
            "swapp_lab_scorer",
        ),
        (
            "lab.recover_holdout_failure(uuid,uuid,integer,bigint,text,text,text,text)",
            "swapp_lab_scorer",
        ),
        ("lab.recover_unclaimed_holdout_failure(uuid,uuid)", "swapp_lab_scorer"),
        ("lab.read_holdout_recovery_target(uuid)", "swapp_lab_scorer"),
        ("lab.list_holdout_recovery_targets(uuid)", "swapp_lab_scorer"),
        ("lab.read_holdout_bit(uuid,uuid)", "swapp_lab_director"),
        ("lab.read_holdout_request(uuid,text,text,text,integer)", "swapp_lab_director"),
        ("lab.read_holdout_run_end_fence(uuid)", "swapp_lab_director"),
        ("lab.read_run_end_unavailable(uuid)", "swapp_lab_director"),
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC,{role}")
    for signature, role in (
        (
            "lab.assert_director_holdout_execution(uuid,integer,text,text)",
            "swapp_lab_director",
        ),
        (
            "lab.assert_director_holdout_closure(uuid,integer,text,text)",
            "swapp_lab_director",
        ),
        (
            "lab.assert_scorer_holdout_execution(uuid,integer,text,text)",
            "swapp_lab_scorer",
        ),
        (
            "lab.reserve_holdout_check(uuid,uuid,text,text,text,integer,integer,text,text)",
            "swapp_lab_director",
        ),
        (
            "lab.register_holdout_run_end_intent(uuid,jsonb,integer,text,text)",
            "swapp_lab_director",
        ),
        (
            "lab.fence_missing_holdout_run_end(uuid,text,integer,text,text,integer,text,text)",
            "swapp_lab_director",
        ),
        (
            "lab.fence_run_end_unavailable(uuid,text,text,text,text,integer,text,text,integer,"
            "double precision,text,integer,integer,text,text)",
            "swapp_lab_director",
        ),
        (
            "lab.fence_run_end_reserved_budget_without_intent(uuid,text,text,text,integer,text,"
            "text,integer,text,text,integer,text,text,integer,integer,text,text)",
            "swapp_lab_director",
        ),
        (
            "lab.claim_holdout_reservation(uuid,integer,bigint,text,text,text,text,integer,text)",
            "swapp_lab_scorer",
        ),
        (
            "lab.check_holdout_admission(uuid,uuid,integer,bigint,text,text,text,text,integer,text)",
            "swapp_lab_scorer",
        ),
        (
            "lab.publish_holdout_result(uuid,double precision,jsonb,text,integer,bigint,text,"
            "text,text,text,integer,text)",
            "swapp_lab_scorer",
        ),
        (
            "lab.fail_holdout_reservation(uuid,text,integer,bigint,text,text,text,text,integer,text,text)",
            "swapp_lab_scorer",
        ),
        (
            "lab.recover_holdout_failure(uuid,uuid,integer,bigint,text,text,text,text,integer,text)",
            "swapp_lab_scorer",
        ),
        (
            "lab.recover_unclaimed_holdout_failure(uuid,uuid,integer,text)",
            "swapp_lab_scorer",
        ),
        ("lab.read_holdout_recovery_target_v2(uuid)", "swapp_lab_scorer"),
        ("lab.list_holdout_recovery_targets_v2(uuid)", "swapp_lab_scorer"),
        ("lab.read_holdout_bit_v2(uuid,uuid)", "swapp_lab_director"),
        ("lab.read_holdout_request_v2(uuid,text,text,text,integer)", "swapp_lab_director"),
        ("lab.read_holdout_run_end_fence_v2(uuid)", "swapp_lab_director"),
        ("lab.read_run_end_unavailable_v2(uuid)", "swapp_lab_director"),
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {role}")
    op.execute(
        "REVOKE ALL ON FUNCTION lab.guard_holdout_owner_pair() FROM PUBLIC,"
        "swapp_lab_director,swapp_lab_scorer,swapp_lab_planner"
    )
    for function in (
        "lab.guard_director_holdout_fence_statement()",
        "lab.guard_director_holdout_fence_row()",
    ):
        op.execute(
            f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC,"
            "swapp_lab_director,swapp_lab_scorer,swapp_lab_planner"
        )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "lab.assert_director_holdout_checkpoint(uuid,integer,text,text,"
        "text,text,integer,text,jsonb) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "lab.assert_director_holdout_checkpoint(uuid,integer,text,text,"
        "text,text,integer,text,jsonb) "
        "TO swapp_lab_director"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "lab.assert_director_holdout_closure(uuid,integer,text,text) FROM PUBLIC"
    )


def downgrade() -> None:
    """Remove owner binding only after callers and active reservations are drained."""
    raise RuntimeError("0026 is additive and intentionally has no automatic downgrade")
