"""Persist immutable Director execution pins and owner generations."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0025_director_generations"
down_revision = "0024_baseline_operation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add atomic generation-1 ownership and a same-transaction owner fence."""
    op.create_table(
        "director_execution_contracts",
        sa.Column("run_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("execution_json", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("execution_sha256", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["lab.runs.run_id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "run_id", "execution_sha256", name="uq_director_execution_contract_digest"
        ),
        sa.CheckConstraint(
            "payload_sha256 ~ '^[0-9a-f]{64}$'", name="ck_director_execution_payload_sha"
        ),
        sa.CheckConstraint("execution_sha256 ~ '^[0-9a-f]{64}$'", name="ck_director_execution_sha"),
        sa.CheckConstraint("deadline_at > started_at", name="ck_director_execution_deadline"),
        schema="lab",
    )
    op.create_table(
        "director_owner_generations",
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("execution_sha256", sa.String(64), nullable=False),
        sa.Column("worker_pid", sa.Integer(), nullable=False),
        sa.Column("worker_start_ticks", sa.BigInteger(), nullable=False),
        sa.Column("worker_boot_id", sa.String(36), nullable=False),
        sa.Column("worker_unit", sa.String(255), nullable=False),
        sa.Column("worker_invocation_id", sa.String(32), nullable=False),
        sa.Column("worker_cgroup", sa.Text(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("restart_id", sa.Uuid(as_uuid=True)),
        sa.ForeignKeyConstraint(
            ["run_id"], ["lab.director_execution_contracts.run_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id", "generation"),
        sa.UniqueConstraint(
            "run_id", "worker_invocation_id", name="uq_director_owner_generation_invocation"
        ),
        sa.CheckConstraint("generation > 0", name="ck_director_owner_generation_positive"),
        sa.CheckConstraint(
            "execution_sha256 ~ '^[0-9a-f]{64}$'", name="ck_director_generation_execution_sha"
        ),
        sa.CheckConstraint(
            "worker_pid > 1 and worker_start_ticks > 0", name="ck_director_generation_process"
        ),
        sa.CheckConstraint(
            "worker_boot_id ~ '^[0-9a-f-]{36}$'", name="ck_director_generation_boot"
        ),
        sa.CheckConstraint(
            "worker_invocation_id ~ '^[0-9a-f]{32}$'", name="ck_director_generation_invocation"
        ),
        sa.CheckConstraint(
            "worker_unit ~ '^[A-Za-z0-9_.@-]+[.]service$'", name="ck_director_generation_unit"
        ),
        sa.CheckConstraint(
            "worker_cgroup ~ '^/.+[.]service$'", name="ck_director_generation_cgroup"
        ),
        sa.CheckConstraint(
            "worker_cgroup like '%/' || worker_unit", name="ck_director_generation_unit_cgroup"
        ),
        sa.CheckConstraint(
            "(generation = 1 and restart_id is null) or "
            "(generation > 1 and restart_id is not null)",
            name="ck_director_generation_restart",
        ),
        schema="lab",
    )
    op.create_table(
        "director_execution_control",
        sa.Column("run_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("current_generation", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False, server_default="active"),
        sa.Column("restart_id", sa.Uuid(as_uuid=True)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["lab.director_execution_contracts.run_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "current_generation"],
            ["lab.director_owner_generations.run_id", "lab.director_owner_generations.generation"],
        ),
        sa.CheckConstraint("current_generation > 0", name="ck_director_control_generation"),
        sa.CheckConstraint("mode in ('active','reconciling')", name="ck_director_control_mode"),
        sa.CheckConstraint(
            "(mode = 'active' and restart_id is null) or "
            "(mode = 'reconciling' and restart_id is not null)",
            name="ck_director_control_restart",
        ),
        schema="lab",
    )
    op.create_table(
        "director_restart_requests",
        sa.Column("restart_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("expected_generation", sa.Integer(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("request_json", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("execution_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("observation_sha256", sa.String(64)),
        sa.Column("claimant_generation", sa.Integer()),
        sa.Column("result_sha256", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "expected_generation"],
            ["lab.director_owner_generations.run_id", "lab.director_owner_generations.generation"],
        ),
        sa.UniqueConstraint(
            "run_id",
            "expected_generation",
            "request_sha256",
            name="uq_director_restart_request_identity",
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'", name="ck_director_restart_request_sha"
        ),
        sa.CheckConstraint(
            "execution_sha256 ~ '^[0-9a-f]{64}$'", name="ck_director_restart_execution_sha"
        ),
        sa.CheckConstraint(
            "purpose in ('continue','stop_and_finalize')", name="ck_director_restart_purpose"
        ),
        sa.CheckConstraint(
            "state in ('pending','drained','claimed','rejected')", name="ck_director_restart_state"
        ),
        schema="lab",
    )

    op.execute(
        "ALTER TABLE lab.director_owner_generations ADD CONSTRAINT "
        "fk_director_generation_contract FOREIGN KEY (run_id,execution_sha256) "
        "REFERENCES lab.director_execution_contracts(run_id,execution_sha256)"
    )
    op.add_column("score_jobs", sa.Column("admitted_generation", sa.Integer()), schema="scorer")
    op.add_column("score_jobs", sa.Column("execution_sha256", sa.String(64)), schema="scorer")
    op.add_column(
        "task_terminal_outcomes", sa.Column("admitted_generation", sa.Integer()), schema="scorer"
    )
    op.add_column(
        "task_terminal_outcomes", sa.Column("execution_sha256", sa.String(64)), schema="scorer"
    )
    op.create_check_constraint(
        "ck_score_jobs_execution_owner_pair",
        "score_jobs",
        "(admitted_generation is null and execution_sha256 is null) or "
        "(admitted_generation > 0 and length(execution_sha256) = 64)",
        schema="scorer",
    )
    op.create_check_constraint(
        "ck_score_jobs_execution_sha",
        "score_jobs",
        "execution_sha256 ~ '^[0-9a-f]{64}$'",
        schema="scorer",
    )
    op.create_check_constraint(
        "ck_terminal_execution_owner_pair",
        "task_terminal_outcomes",
        "(admitted_generation is null and execution_sha256 is null) or "
        "(admitted_generation > 0 and length(execution_sha256) = 64)",
        schema="scorer",
    )
    op.create_check_constraint(
        "ck_terminal_execution_sha",
        "task_terminal_outcomes",
        "execution_sha256 ~ '^[0-9a-f]{64}$'",
        schema="scorer",
    )

    op.execute(
        """
        CREATE FUNCTION lab.claim_initial_director_execution(
            p_run_id uuid, p_payload_sha256 text, p_execution_json jsonb,
            p_execution_sha256 text, p_wall_seconds integer, p_worker_pid integer,
            p_worker_start_ticks bigint, p_worker_boot_id text, p_worker_unit text,
            p_worker_invocation_id text, p_worker_cgroup text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE current_run lab.runs%ROWTYPE; started timestamptz; owner_generation integer;
            request_wall_seconds integer; execution_wall_seconds integer;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'initial Director claim requires the Director role';
            END IF;
            IF p_run_id IS NULL OR p_payload_sha256 IS NULL OR
               p_payload_sha256 !~ '^[0-9a-f]{64}$' OR p_execution_sha256 IS NULL OR
               p_execution_sha256 !~ '^[0-9a-f]{64}$' OR
               p_wall_seconds IS NULL OR p_wall_seconds < 1 OR p_wall_seconds > 14400 OR
               p_execution_json IS NULL OR p_worker_pid IS NULL OR p_worker_pid <= 1 OR
               p_worker_start_ticks IS NULL OR p_worker_start_ticks <= 0 OR
               p_worker_boot_id IS NULL OR p_worker_boot_id !~ '^[0-9a-f-]{36}$' OR
               p_worker_unit IS NULL OR p_worker_unit !~ '^[A-Za-z0-9_.@-]+[.]service$' OR
               p_worker_invocation_id IS NULL OR p_worker_invocation_id !~ '^[0-9a-f]{32}$' OR
               p_worker_cgroup IS NULL OR p_worker_cgroup !~ '^/.+[.]service$' OR
               p_worker_cgroup NOT LIKE '%/' || p_worker_unit THEN
                RAISE EXCEPTION 'initial Director claim identity is malformed';
            END IF;
            IF p_execution_json->>'schema' IS DISTINCT FROM 'director-execution-contract.v1' OR
               p_execution_json->>'run_id' IS DISTINCT FROM p_run_id::text OR
               p_execution_json->>'request_sha256' IS DISTINCT FROM p_payload_sha256 OR
               jsonb_typeof(p_execution_json->'wall_seconds') IS DISTINCT FROM 'number' THEN
                RAISE EXCEPTION 'execution contract does not match initial claim';
            END IF;
            PERFORM lab.lock_run_plan(p_run_id);
            SELECT * INTO current_run FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
            IF jsonb_typeof(current_run.request_json->'budget') IS DISTINCT FROM 'object' OR
               jsonb_typeof(current_run.request_json->'budget'->'wall_seconds')
                   IS DISTINCT FROM 'number' THEN
                RAISE EXCEPTION 'immutable request wall budget is malformed';
            END IF;
            BEGIN
                request_wall_seconds :=
                    (current_run.request_json->'budget'->>'wall_seconds')::integer;
                execution_wall_seconds := (p_execution_json->>'wall_seconds')::integer;
            EXCEPTION WHEN OTHERS THEN
                RAISE EXCEPTION 'immutable request wall budget is malformed';
            END;
            IF NOT FOUND OR current_run.state <> 'queued' OR current_run.stop_requested OR
               current_run.payload_sha256 IS DISTINCT FROM p_payload_sha256 OR
               p_execution_json->'request' IS DISTINCT FROM
                   (current_run.request_json - 'idempotency_key') OR
               request_wall_seconds IS DISTINCT FROM p_wall_seconds OR
               execution_wall_seconds IS DISTINCT FROM request_wall_seconds THEN
                RAISE EXCEPTION 'run is not eligible for an initial Director claim';
            END IF;
            IF EXISTS (SELECT 1 FROM lab.director_execution_contracts WHERE run_id=p_run_id) OR
               EXISTS (SELECT 1 FROM lab.director_execution_control WHERE run_id=p_run_id) THEN
                RAISE EXCEPTION 'run already has an immutable execution claim';
            END IF;
            PERFORM set_config('lab.initial_director_claim',p_run_id::text,true);
            started := clock_timestamp();
            INSERT INTO lab.director_execution_contracts(
                run_id,payload_sha256,execution_json,execution_sha256,started_at,deadline_at
            ) VALUES (
                p_run_id,p_payload_sha256,p_execution_json,p_execution_sha256,started,
                started + make_interval(secs => p_wall_seconds)
            );
            owner_generation := 1;
            INSERT INTO lab.director_owner_generations(
                run_id,generation,execution_sha256,worker_pid,worker_start_ticks,
                worker_boot_id,worker_unit,worker_invocation_id,worker_cgroup,claimed_at
            ) VALUES (
                p_run_id,owner_generation,p_execution_sha256,p_worker_pid,p_worker_start_ticks,
                p_worker_boot_id,p_worker_unit,p_worker_invocation_id,p_worker_cgroup,started
            );
            INSERT INTO lab.director_execution_control(run_id,current_generation,mode)
            VALUES (p_run_id,owner_generation,'active');
            UPDATE lab.runs SET state='running',updated_at=started WHERE run_id=p_run_id;
            INSERT INTO lab.director_run_owners(
                run_id,payload_sha256,worker_pid,worker_start_ticks,worker_boot_id,
                worker_unit,worker_invocation_id,worker_cgroup,claimed_at
            ) VALUES (
                p_run_id,p_payload_sha256,p_worker_pid,p_worker_start_ticks,p_worker_boot_id,
                p_worker_unit,p_worker_invocation_id,p_worker_cgroup,started
            );
            INSERT INTO lab.run_events(event_id,run_id,event_type,event_json,created_at)
            VALUES (
                gen_random_uuid(),p_run_id,'run.started',
                jsonb_build_object('payload_sha256',p_payload_sha256,'dispatcher','director.v2'),
                started
            );
            RETURN jsonb_build_object(
                'state','running','newly_claimed',true,'generation',owner_generation,
                'worker_invocation_id',p_worker_invocation_id,
                'execution_sha256',p_execution_sha256,'started_at',started,'deadline_at',
                started + make_interval(secs => p_wall_seconds)
            );
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.assert_director_owner_context(p_run_id uuid)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE expected_run uuid; expected_generation integer; expected_invocation text;
            expected_execution text; current_run lab.runs%ROWTYPE;
            control_row lab.director_execution_control%ROWTYPE;
            contract_row lab.director_execution_contracts%ROWTYPE; lock_key bigint;
        BEGIN
            IF session_user NOT IN ('swapp_lab_director','swapp_lab_planner') OR
               p_run_id IS NULL THEN
                RAISE EXCEPTION
                    'Director owner assertion requires a valid Director or Planner identity';
            END IF;
            BEGIN
                expected_run := nullif(current_setting('lab.owner_run_id',true),'')::uuid;
                expected_generation := nullif(
                    current_setting('lab.owner_generation',true),'')::integer;
            EXCEPTION WHEN OTHERS THEN
                RAISE EXCEPTION 'Director owner context is malformed';
            END;
            expected_invocation := nullif(current_setting('lab.owner_invocation_id',true),'');
            expected_execution := nullif(current_setting('lab.owner_execution_sha256',true),'');
            IF expected_run IS NULL OR expected_generation IS NULL OR
               expected_invocation IS NULL OR expected_execution IS NULL OR
               expected_run IS DISTINCT FROM p_run_id THEN
                RAISE EXCEPTION 'Director owner context is missing or bound to another run';
            END IF;
            lock_key := ('x' || substr(encode(sha256(uuid_send(p_run_id)), 'hex'), 1, 16))
                ::bit(64)::bigint;
            PERFORM pg_advisory_xact_lock(lock_key);
            SELECT * INTO current_run FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
            SELECT * INTO control_row FROM lab.director_execution_control
             WHERE run_id=p_run_id FOR UPDATE;
            SELECT * INTO contract_row FROM lab.director_execution_contracts
             WHERE run_id=p_run_id;
            IF NOT FOUND OR current_run.state <> 'running' OR current_run.stop_requested OR
               control_row.mode <> 'active' OR
               control_row.current_generation IS DISTINCT FROM expected_generation OR
               contract_row.execution_sha256 IS DISTINCT FROM expected_execution OR
               contract_row.deadline_at <= clock_timestamp() OR NOT EXISTS (
                   SELECT 1 FROM lab.director_owner_generations AS owner
                    WHERE owner.run_id=p_run_id
                      AND owner.generation=expected_generation
                      AND owner.worker_invocation_id=expected_invocation
                      AND owner.execution_sha256=expected_execution
               ) THEN
                RAISE EXCEPTION 'Director owner generation is stale or execution is inactive';
            END IF;
        END;
        $$
        """
    )
    # Existing lifecycle RPCs acquire this lock before locking experiment/job rows.
    # Bind that lock to the actual target before any child row can be touched.
    op.execute(
        """
        CREATE FUNCTION lab.assert_director_generation_identity(
            p_run_id uuid, p_generation integer, p_invocation_id text,
            p_execution_sha256 text
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE lock_key bigint; run_item lab.runs%ROWTYPE;
            control_item lab.director_execution_control%ROWTYPE;
            contract_item lab.director_execution_contracts%ROWTYPE;
        BEGIN
            IF session_user NOT IN ('swapp_lab_director','swapp_lab_planner') OR
               p_run_id IS NULL OR p_generation IS NULL OR p_generation < 1 OR
               p_invocation_id IS NULL OR p_invocation_id !~ '^[0-9a-f]{32}$' OR
               p_execution_sha256 IS NULL OR p_execution_sha256 !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'Director generation identity is malformed';
            END IF;
            lock_key := ('x' || substr(encode(sha256(uuid_send(p_run_id)), 'hex'), 1, 16))
                ::bit(64)::bigint;
            PERFORM pg_advisory_xact_lock(lock_key);
            SELECT * INTO run_item FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
            SELECT * INTO control_item FROM lab.director_execution_control
             WHERE run_id=p_run_id FOR UPDATE;
            SELECT * INTO contract_item FROM lab.director_execution_contracts
             WHERE run_id=p_run_id;
            IF run_item.run_id IS NULL OR run_item.state NOT IN ('running','stop_requested') OR
               control_item.run_id IS NULL OR control_item.mode <> 'active' OR
               control_item.current_generation IS DISTINCT FROM p_generation OR
               contract_item.run_id IS NULL OR
               contract_item.execution_sha256 IS DISTINCT FROM p_execution_sha256 OR
               contract_item.payload_sha256 IS DISTINCT FROM run_item.payload_sha256 OR
               NOT EXISTS (
                   SELECT 1 FROM lab.director_owner_generations AS owner
                    WHERE owner.run_id=p_run_id AND owner.generation=p_generation
                      AND owner.worker_invocation_id=p_invocation_id
                      AND owner.execution_sha256=p_execution_sha256
               ) THEN
                RAISE EXCEPTION 'Director generation is no longer current for this run';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.assert_planner_job_stop_execution(p_job_id uuid)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE target_run uuid; run_item lab.runs%ROWTYPE;
            control_item lab.director_execution_control%ROWTYPE;
            contract_item lab.director_execution_contracts%ROWTYPE;
            job_item scorer.score_jobs%ROWTYPE; invocation text;
        BEGIN
            IF session_user <> 'swapp_lab_planner' OR p_job_id IS NULL THEN
                RAISE EXCEPTION 'Planner stop cancellation requires one captured queued job';
            END IF;
            SELECT run_id INTO target_run FROM scorer.score_jobs WHERE job_id=p_job_id;
            IF target_run IS NULL THEN
                RAISE EXCEPTION 'Planner stop cancellation job does not exist';
            END IF;
            PERFORM lab.lock_run_plan(target_run);
            SELECT * INTO run_item FROM lab.runs WHERE run_id=target_run FOR UPDATE;
            SELECT * INTO control_item FROM lab.director_execution_control
             WHERE run_id=target_run FOR UPDATE;
            SELECT * INTO contract_item FROM lab.director_execution_contracts
             WHERE run_id=target_run;
            SELECT * INTO job_item FROM scorer.score_jobs WHERE job_id=p_job_id FOR UPDATE;
            IF run_item.state IS DISTINCT FROM 'stop_requested' OR
               NOT run_item.stop_requested OR job_item.run_id IS DISTINCT FROM target_run OR
               job_item.state IS DISTINCT FROM 'queued' OR
               job_item.admitted_generation IS NULL OR job_item.admitted_generation < 1 OR
               job_item.execution_sha256 IS NULL OR
               control_item.mode IS DISTINCT FROM 'active' OR
               control_item.current_generation IS DISTINCT FROM job_item.admitted_generation OR
               contract_item.execution_sha256 IS DISTINCT FROM job_item.execution_sha256 THEN
                RAISE EXCEPTION
                    'queued job is not authorized for current-generation stop cancellation';
            END IF;
            SELECT owner.worker_invocation_id INTO invocation
              FROM lab.director_owner_generations AS owner
             WHERE owner.run_id=target_run
               AND owner.generation=job_item.admitted_generation
               AND owner.execution_sha256=job_item.execution_sha256;
            IF invocation IS NULL THEN
                RAISE EXCEPTION 'queued job owner generation history is missing';
            END IF;
            PERFORM set_config('lab.owner_run_id',target_run::text,true);
            PERFORM set_config('lab.owner_generation',job_item.admitted_generation::text,true);
            PERFORM set_config('lab.owner_invocation_id',invocation,true);
            PERFORM set_config('lab.owner_execution_sha256',job_item.execution_sha256,true);
            PERFORM lab.assert_director_generation_identity(
                target_run,job_item.admitted_generation,invocation,job_item.execution_sha256
            );
            RETURN jsonb_build_object(
                'run_id',target_run,'admitted_generation',job_item.admitted_generation,
                'execution_sha256',job_item.execution_sha256
            );
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION lab.lock_run_plan(p_run_id uuid)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE lock_key bigint; owner_run uuid;
        BEGIN
            IF session_user NOT IN ('swapp_lab_director','swapp_lab_planner') OR
               p_run_id IS NULL THEN
                RAISE EXCEPTION 'run lifecycle lock requires Director or Planner identity';
            END IF;
            BEGIN
                owner_run := nullif(current_setting('lab.owner_run_id',true),'')::uuid;
            EXCEPTION WHEN OTHERS THEN
                RAISE EXCEPTION 'Director transaction owner target is malformed';
            END;
            IF owner_run IS NOT NULL AND owner_run IS DISTINCT FROM p_run_id THEN
                RAISE EXCEPTION 'run lifecycle lock target differs from captured owner';
            END IF;
            lock_key := ('x' || substr(encode(sha256(uuid_send(p_run_id)), 'hex'), 1, 16))
                ::bit(64)::bigint;
            PERFORM pg_advisory_xact_lock(lock_key);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.assert_scorer_run_execution(
            p_run_id uuid,p_generation integer,p_execution_sha256 text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE lock_key bigint; run_item lab.runs%ROWTYPE;
            control_item lab.director_execution_control%ROWTYPE;
            contract_item lab.director_execution_contracts%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' OR p_run_id IS NULL OR
               p_generation IS NULL OR p_generation < 1 OR
               p_execution_sha256 IS NULL OR p_execution_sha256 !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'Scorer execution identity is malformed';
            END IF;
            lock_key := ('x' || substr(encode(sha256(uuid_send(p_run_id)), 'hex'), 1, 16))
                ::bit(64)::bigint;
            PERFORM pg_advisory_xact_lock(lock_key);
            SELECT * INTO run_item FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
            SELECT * INTO control_item FROM lab.director_execution_control
             WHERE run_id=p_run_id FOR UPDATE;
            SELECT * INTO contract_item FROM lab.director_execution_contracts
             WHERE run_id=p_run_id;
            IF run_item.run_id IS NULL OR run_item.state IS DISTINCT FROM 'running' OR
               run_item.stop_requested OR control_item.run_id IS NULL OR
               control_item.mode IS DISTINCT FROM 'active' OR
               control_item.current_generation IS DISTINCT FROM p_generation OR
               contract_item.run_id IS NULL OR
               contract_item.execution_sha256 IS DISTINCT FROM p_execution_sha256 OR
               contract_item.payload_sha256 IS DISTINCT FROM run_item.payload_sha256 OR
               contract_item.deadline_at <= clock_timestamp() OR NOT EXISTS (
                   SELECT 1 FROM lab.director_owner_generations AS owner
                    WHERE owner.run_id=p_run_id AND owner.generation=p_generation
                      AND owner.execution_sha256=p_execution_sha256
               ) THEN
                RAISE EXCEPTION 'Scorer execution owner is stale or inactive';
            END IF;
            PERFORM set_config('lab.scorer_run_id',p_run_id::text,true);
            PERFORM set_config('lab.scorer_generation',p_generation::text,true);
            PERFORM set_config('lab.scorer_execution_sha256',p_execution_sha256,true);
            PERFORM set_config('lab.scorer_stop_closure','false',true);
            RETURN jsonb_build_object('run_id',p_run_id,'admitted_generation',p_generation,
                'execution_sha256',p_execution_sha256);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.assert_scorer_run_stop_execution(
            p_run_id uuid,p_generation integer,p_execution_sha256 text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE lock_key bigint; run_item lab.runs%ROWTYPE;
            control_item lab.director_execution_control%ROWTYPE;
            contract_item lab.director_execution_contracts%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' OR p_run_id IS NULL OR
               p_generation IS NULL OR p_generation < 1 OR
               p_execution_sha256 IS NULL OR p_execution_sha256 !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'Scorer stop execution identity is malformed';
            END IF;
            lock_key := ('x' || substr(encode(sha256(uuid_send(p_run_id)), 'hex'), 1, 16))
                ::bit(64)::bigint;
            PERFORM pg_advisory_xact_lock(lock_key);
            SELECT * INTO run_item FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
            SELECT * INTO control_item FROM lab.director_execution_control
             WHERE run_id=p_run_id FOR UPDATE;
            SELECT * INTO contract_item FROM lab.director_execution_contracts
             WHERE run_id=p_run_id;
            IF run_item.run_id IS NULL OR run_item.state IS DISTINCT FROM 'stop_requested' OR
               NOT run_item.stop_requested OR control_item.run_id IS NULL OR
               control_item.mode IS DISTINCT FROM 'active' OR
               control_item.current_generation IS DISTINCT FROM p_generation OR
               contract_item.execution_sha256 IS DISTINCT FROM p_execution_sha256 OR
               contract_item.payload_sha256 IS DISTINCT FROM run_item.payload_sha256 OR
               NOT EXISTS (
                   SELECT 1 FROM lab.director_owner_generations AS owner
                    WHERE owner.run_id=p_run_id AND owner.generation=p_generation
                      AND owner.execution_sha256=p_execution_sha256
               ) THEN
                RAISE EXCEPTION 'Scorer stop owner is stale or not authorized';
            END IF;
            PERFORM set_config('lab.scorer_run_id',p_run_id::text,true);
            PERFORM set_config('lab.scorer_generation',p_generation::text,true);
            PERFORM set_config('lab.scorer_execution_sha256',p_execution_sha256,true);
            PERFORM set_config('lab.scorer_stop_closure','true',true);
            RETURN jsonb_build_object('run_id',p_run_id,'admitted_generation',p_generation,
                'execution_sha256',p_execution_sha256,'mode','stop_closure');
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.assert_scorer_job_execution(
            p_job_id uuid,p_claim_token text,p_invocation_id text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE initial_job scorer.score_jobs%ROWTYPE; locked_job scorer.score_jobs%ROWTYPE;
            assertion jsonb;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' OR p_job_id IS NULL OR
               (p_claim_token IS NULL) IS DISTINCT FROM (p_invocation_id IS NULL) THEN
                RAISE EXCEPTION 'Scorer score-job assertion is malformed';
            END IF;
            SELECT * INTO initial_job FROM scorer.score_jobs WHERE job_id=p_job_id;
            IF initial_job.job_id IS NULL OR initial_job.admitted_generation IS NULL OR
               initial_job.execution_sha256 IS NULL THEN
                RAISE EXCEPTION 'score job has no admitted Director generation';
            END IF;
            assertion := lab.assert_scorer_run_execution(
                initial_job.run_id,initial_job.admitted_generation,
                initial_job.execution_sha256
            );
            SELECT * INTO locked_job FROM scorer.score_jobs
             WHERE job_id=p_job_id FOR UPDATE;
            IF locked_job.job_id IS NULL OR locked_job.run_id IS DISTINCT FROM initial_job.run_id OR
               locked_job.admitted_generation IS DISTINCT FROM initial_job.admitted_generation OR
               locked_job.execution_sha256 IS DISTINCT FROM initial_job.execution_sha256 THEN
                RAISE EXCEPTION 'score-job generation changed during assertion';
            END IF;
            IF p_claim_token IS NULL THEN
                IF locked_job.state NOT IN ('queued','running') OR
                   (locked_job.state='running' AND locked_job.lease_until > clock_timestamp()) THEN
                    RAISE EXCEPTION 'score job is not available for a generation-bound claim';
                END IF;
            ELSIF p_claim_token !~ '^[A-Za-z0-9_.:-]{1,128}$' OR
                  p_invocation_id !~ '^[0-9a-f]{32}$' OR
                  locked_job.state IS DISTINCT FROM 'running' OR
                  locked_job.claimed_by IS DISTINCT FROM p_claim_token OR
                  locked_job.claim_invocation_id IS DISTINCT FROM p_invocation_id OR
                  locked_job.lease_until <= clock_timestamp() THEN
                RAISE EXCEPTION 'score-job worker claim is stale';
            END IF;
            PERFORM set_config('lab.scorer_job_id',p_job_id::text,true);
            IF p_claim_token IS NULL THEN
                PERFORM set_config('lab.scorer_job_transition','claim',true);
                PERFORM set_config('lab.scorer_claim_token','',true);
                PERFORM set_config('lab.scorer_worker_invocation_id','',true);
            ELSE
                PERFORM set_config('lab.scorer_job_transition','worker',true);
                PERFORM set_config('lab.scorer_claim_token',p_claim_token,true);
                PERFORM set_config('lab.scorer_worker_invocation_id',p_invocation_id,true);
            END IF;
            RETURN assertion;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.assert_scorer_job_stop_execution(
            p_job_id uuid,p_claim_token text,p_invocation_id text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE initial_job scorer.score_jobs%ROWTYPE; locked_job scorer.score_jobs%ROWTYPE;
            assertion jsonb;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' OR p_job_id IS NULL OR
               p_claim_token IS NULL OR p_claim_token !~ '^[A-Za-z0-9_.:-]{1,128}$' OR
               p_invocation_id IS NULL OR p_invocation_id !~ '^[0-9a-f]{32}$' THEN
                RAISE EXCEPTION 'Scorer stop score-job assertion is malformed';
            END IF;
            SELECT * INTO initial_job FROM scorer.score_jobs WHERE job_id=p_job_id;
            IF initial_job.job_id IS NULL OR initial_job.admitted_generation IS NULL OR
               initial_job.execution_sha256 IS NULL THEN
                RAISE EXCEPTION 'score job has no admitted Director generation';
            END IF;
            assertion := lab.assert_scorer_run_stop_execution(
                initial_job.run_id,initial_job.admitted_generation,
                initial_job.execution_sha256
            );
            SELECT * INTO locked_job FROM scorer.score_jobs
             WHERE job_id=p_job_id FOR UPDATE;
            IF locked_job.job_id IS NULL OR locked_job.run_id IS DISTINCT FROM initial_job.run_id OR
               locked_job.admitted_generation IS DISTINCT FROM initial_job.admitted_generation OR
               locked_job.execution_sha256 IS DISTINCT FROM initial_job.execution_sha256 OR
               locked_job.state IS DISTINCT FROM 'running' OR
               locked_job.claimed_by IS DISTINCT FROM p_claim_token OR
               locked_job.claim_invocation_id IS DISTINCT FROM p_invocation_id THEN
                RAISE EXCEPTION 'stopped score-job worker claim is stale';
            END IF;
            PERFORM set_config('lab.scorer_job_id',p_job_id::text,true);
            PERFORM set_config('lab.scorer_job_transition','cancel',true);
            PERFORM set_config('lab.scorer_claim_token',p_claim_token,true);
            PERFORM set_config('lab.scorer_worker_invocation_id',p_invocation_id,true);
            RETURN assertion;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.close_director_run_if_owned(
            p_run_id uuid, p_generation integer, p_invocation_id text,
            p_execution_sha256 text, p_target_state text,
            p_error_type text, p_failure_reason text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE run_item lab.runs%ROWTYPE; control_item lab.director_execution_control%ROWTYPE;
            lock_key bigint; event_type text;
        BEGIN
            IF session_user <> 'swapp_lab_director' OR p_run_id IS NULL OR
               p_generation IS NULL OR p_generation < 1 OR
               p_invocation_id IS NULL OR p_invocation_id !~ '^[0-9a-f]{32}$' OR
               p_execution_sha256 IS NULL OR p_execution_sha256 !~ '^[0-9a-f]{64}$' OR
               p_target_state IS NULL OR p_target_state NOT IN ('failed','stop_requested') OR
               p_error_type IS NULL OR p_error_type !~ '^[A-Za-z0-9_.]{1,128}$' OR
               p_failure_reason IS NULL OR length(p_failure_reason) > 160 OR
               p_failure_reason ~ '[^ -~]' THEN
                RAISE EXCEPTION 'Director closure request is malformed';
            END IF;
            lock_key := ('x' || substr(encode(sha256(uuid_send(p_run_id)), 'hex'), 1, 16))
                ::bit(64)::bigint;
            PERFORM pg_advisory_xact_lock(lock_key);
            SELECT * INTO run_item FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
            SELECT * INTO control_item FROM lab.director_execution_control
             WHERE run_id=p_run_id FOR UPDATE;
            IF NOT FOUND OR run_item.state <> 'running' OR
               control_item.mode <> 'active' OR
               control_item.current_generation IS DISTINCT FROM p_generation OR
               NOT EXISTS (
                   SELECT 1 FROM lab.director_owner_generations AS owner
                    WHERE owner.run_id=p_run_id AND owner.generation=p_generation
                      AND owner.worker_invocation_id=p_invocation_id
                      AND owner.execution_sha256=p_execution_sha256
               ) OR NOT EXISTS (
                   SELECT 1 FROM lab.director_execution_contracts AS contract
                    WHERE contract.run_id=p_run_id
                      AND contract.execution_sha256=p_execution_sha256
               ) THEN
                RAISE EXCEPTION 'Director closure owner is stale or run is inactive';
            END IF;
            PERFORM set_config('lab.closure_run_id',p_run_id::text,true);
            PERFORM set_config('lab.closure_generation',p_generation::text,true);
            PERFORM set_config('lab.closure_invocation_id',p_invocation_id,true);
            PERFORM set_config('lab.closure_execution_sha256',p_execution_sha256,true);
            UPDATE lab.runs SET state=p_target_state,
                stop_requested=(p_target_state='stop_requested'),updated_at=clock_timestamp()
             WHERE run_id=p_run_id AND state='running';
            event_type := CASE WHEN p_target_state='failed' THEN 'run.failed'
                               ELSE 'run.dispatch_recovery_required' END;
            INSERT INTO lab.run_events(event_id,run_id,event_type,event_json,created_at)
            VALUES (
                gen_random_uuid(),p_run_id,event_type,
                jsonb_build_object('dispatcher','director.v2','error_type',p_error_type,
                    'reason',p_failure_reason),clock_timestamp()
            );
            RETURN jsonb_build_object('state',p_target_state,'event_type',event_type);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_director_execution_claim()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.state = 'queued' AND NEW.state = 'running' THEN
                IF current_setting('lab.initial_director_claim',true)
                        IS DISTINCT FROM NEW.run_id::text OR
                   NOT EXISTS (
                       SELECT 1 FROM lab.director_execution_contracts WHERE run_id=NEW.run_id
                   ) OR
                   NOT EXISTS (
                       SELECT 1 FROM lab.director_execution_control
                        WHERE run_id=NEW.run_id AND current_generation=1 AND mode='active'
                   ) THEN
                    RAISE EXCEPTION 'queued run requires an atomic generation-1 Director claim';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER director_execution_claim_guard BEFORE UPDATE OF state ON lab.runs "
        "FOR EACH ROW EXECUTE FUNCTION lab.guard_director_execution_claim()"
    )
    op.execute(
        """
        CREATE FUNCTION lab.assert_scorer_empty_baseline_stop(p_run_id uuid)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE run_item lab.runs%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' OR p_run_id IS NULL THEN
                RAISE EXCEPTION 'empty baseline stop requires Scorer identity';
            END IF;
            PERFORM pg_advisory_xact_lock(
                ('x' || substr(encode(sha256(uuid_send(p_run_id)), 'hex'), 1, 16))
                    ::bit(64)::bigint
            );
            SELECT * INTO run_item FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
            IF run_item.run_id IS NULL OR
               run_item.state NOT IN ('stop_requested','stopped') OR
               run_item.request_json->>'purpose' IS DISTINCT FROM 'baseline' OR
               run_item.request_json->>'proposal_limit' IS DISTINCT FROM '0' OR
               run_item.request_json->'budget'->>'experiments' IS DISTINCT FROM '0' OR
               run_item.request_json->'budget'->>'model_tokens' IS DISTINCT FROM '0' OR
               coalesce(run_item.task_plan_count,0) <> 0 OR
               EXISTS (SELECT 1 FROM lab.director_run_owners WHERE run_id=p_run_id) OR
               EXISTS (SELECT 1 FROM lab.director_execution_contracts WHERE run_id=p_run_id) OR
               EXISTS (SELECT 1 FROM lab.director_owner_generations WHERE run_id=p_run_id) OR
               EXISTS (SELECT 1 FROM lab.director_execution_control WHERE run_id=p_run_id) OR
               EXISTS (SELECT 1 FROM lab.director_restart_requests WHERE run_id=p_run_id) OR
               EXISTS (SELECT 1 FROM lab.baseline_operations WHERE run_id=p_run_id) OR
               EXISTS (SELECT 1 FROM lab.experiments WHERE run_id=p_run_id) OR
               EXISTS (SELECT 1 FROM lab.baseline_calibrations WHERE run_id=p_run_id) OR
               EXISTS (SELECT 1 FROM scorer.run_tasks WHERE run_id=p_run_id) OR
               EXISTS (SELECT 1 FROM scorer.score_jobs WHERE run_id=p_run_id) OR
               EXISTS (SELECT 1 FROM scorer.task_scores WHERE run_id=p_run_id) OR
               EXISTS (SELECT 1 FROM scorer.task_terminal_outcomes WHERE run_id=p_run_id) OR
               EXISTS (SELECT 1 FROM scorer.task_completions WHERE run_id=p_run_id) OR
               EXISTS (SELECT 1 FROM lab.holdout_reservations WHERE run_id=p_run_id) THEN
                RAISE EXCEPTION 'baseline run has execution evidence or is not an empty stop';
            END IF;
            PERFORM set_config('lab.scorer_baseline_empty_stop_run_id',p_run_id::text,true);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_director_run_update_statement()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE target_run uuid;
        BEGIN
            IF session_user='swapp_lab_migrator' THEN
                RETURN NULL;
            END IF;
            IF session_user='swapp_lab_scorer' THEN
                BEGIN
                    target_run := nullif(current_setting('lab.scorer_run_id',true),'')::uuid;
                EXCEPTION WHEN OTHERS THEN
                    RAISE EXCEPTION 'Scorer run update context is malformed';
                END;
                IF target_run IS NULL THEN
                    BEGIN
                        target_run := nullif(
                            current_setting('lab.scorer_baseline_empty_stop_run_id',true),''
                        )::uuid;
                    EXCEPTION WHEN OTHERS THEN
                        RAISE EXCEPTION 'Scorer baseline stop context is malformed';
                    END;
                    PERFORM lab.assert_scorer_empty_baseline_stop(target_run);
                    RETURN NULL;
                END IF;
                IF current_setting('lab.scorer_stop_closure',true)='true' THEN
                    PERFORM lab.assert_scorer_run_stop_execution(
                        target_run,
                        nullif(current_setting('lab.scorer_generation',true),'')::integer,
                        nullif(current_setting('lab.scorer_execution_sha256',true),'')
                    );
                ELSE
                    PERFORM lab.assert_scorer_run_execution(
                        target_run,
                        nullif(current_setting('lab.scorer_generation',true),'')::integer,
                        nullif(current_setting('lab.scorer_execution_sha256',true),'')
                    );
                END IF;
                RETURN NULL;
            END IF;
            IF session_user NOT IN ('swapp_lab_director','swapp_lab_planner') THEN
                RAISE EXCEPTION 'run update requires the Director or Planner role';
            END IF;
            BEGIN
                target_run := coalesce(
                    nullif(current_setting('lab.owner_run_id',true),'')::uuid,
                    nullif(current_setting('lab.initial_director_claim',true),'')::uuid,
                    nullif(current_setting('lab.closure_run_id',true),'')::uuid,
                    nullif(current_setting('lab.api_stop_run_id',true),'')::uuid
                );
            EXCEPTION WHEN OTHERS THEN
                RAISE EXCEPTION 'run update has a malformed ownership target';
            END;
            IF target_run IS NULL THEN
                RAISE EXCEPTION 'run update requires an owned or authorized transition';
            END IF;
            PERFORM lab.lock_run_plan(target_run);
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_director_run_update()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        BEGIN
            IF session_user='swapp_lab_migrator' THEN
                RETURN NEW;
            END IF;
            IF session_user='swapp_lab_scorer' THEN
                IF current_setting('lab.scorer_baseline_empty_stop_run_id',true)=
                       NEW.run_id::text AND
                   OLD.state='stop_requested' AND NEW.state='stop_requested' AND
                   OLD.stop_requested AND NEW.stop_requested AND
                   OLD.task_plan_sha256 IS NULL AND
                   (OLD.task_plan_count IS NULL OR OLD.task_plan_count=0) AND
                   NEW.task_plan_sha256=
                       '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945' AND
                   NEW.task_plan_count=0 AND
                   (to_jsonb(NEW) - ARRAY[
                       'task_plan_sha256','task_plan_count','updated_at'
                   ]) IS NOT DISTINCT FROM
                   (to_jsonb(OLD) - ARRAY[
                       'task_plan_sha256','task_plan_count','updated_at'
                   ]) THEN
                    PERFORM lab.assert_scorer_empty_baseline_stop(NEW.run_id);
                    RETURN NEW;
                END IF;
                IF current_setting('lab.scorer_baseline_empty_stop_run_id',true)
                        IS NOT NULL AND
                   current_setting('lab.scorer_baseline_empty_stop_run_id',true)=
                       NEW.run_id::text AND
                   OLD.state='stop_requested' AND NEW.state='stopped' AND
                   OLD.stop_requested AND NOT NEW.stop_requested AND
                   NEW.report_sha256 IS NOT NULL AND
                   (to_jsonb(NEW) - ARRAY['state','stop_requested','report_sha256','updated_at'])
                       IS NOT DISTINCT FROM
                   (to_jsonb(OLD) - ARRAY[
                       'state','stop_requested','report_sha256','updated_at'
                   ]) THEN
                    PERFORM lab.assert_scorer_empty_baseline_stop(NEW.run_id);
                    RETURN NEW;
                END IF;
                IF current_setting('lab.scorer_run_id',true) IS DISTINCT FROM NEW.run_id::text OR
                   (to_jsonb(NEW) - ARRAY['state','stop_requested','report_sha256','updated_at'])
                       IS DISTINCT FROM
                   (to_jsonb(OLD) - ARRAY['state','stop_requested','report_sha256','updated_at']) OR
                   NEW.report_sha256 IS NULL OR NEW.stop_requested THEN
                    RAISE EXCEPTION 'Scorer run publication is not bound to the captured owner';
                END IF;
                IF current_setting('lab.scorer_stop_closure',true)='true' THEN
                    IF OLD.state IS DISTINCT FROM 'stop_requested' OR
                       NEW.state IS DISTINCT FROM 'stopped' THEN
                        RAISE EXCEPTION 'Scorer stop closure has an invalid run transition';
                    END IF;
                ELSIF OLD.state IS DISTINCT FROM 'running' OR
                      NEW.state NOT IN ('completed','failed') THEN
                    RAISE EXCEPTION 'Scorer active publication has an invalid run transition';
                END IF;
                RETURN NEW;
            END IF;
            IF session_user NOT IN ('swapp_lab_director','swapp_lab_planner') THEN
                RAISE EXCEPTION 'run update requires the Director or Planner role';
            END IF;
            IF OLD.state='queued' AND NEW.state='running' AND
               current_setting('lab.initial_director_claim',true)=NEW.run_id::text THEN
                RETURN NEW;
            END IF;
            IF OLD.state IN ('queued','running') AND NEW.state='stop_requested' AND
               NEW.stop_requested AND
               current_setting('lab.api_stop_run_id',true)=NEW.run_id::text AND
               (to_jsonb(NEW) - ARRAY['state','stop_requested','updated_at']) IS NOT DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['state','stop_requested','updated_at']) THEN
                RETURN NEW;
            END IF;
            IF OLD.state='running' AND NEW.state IN ('failed','stop_requested') AND
               current_setting('lab.closure_run_id',true)=NEW.run_id::text THEN
                PERFORM lab.assert_director_generation_identity(
                    NEW.run_id,
                    nullif(current_setting('lab.closure_generation',true),'')::integer,
                    nullif(current_setting('lab.closure_invocation_id',true),''),
                    nullif(current_setting('lab.closure_execution_sha256',true),'')
                );
                RETURN NEW;
            END IF;
            IF OLD.state='stop_requested' AND NEW.state='stop_requested' AND
               OLD.stop_requested AND NEW.stop_requested AND
               OLD.task_plan_sha256 IS NULL AND NEW.task_plan_sha256 IS NOT NULL AND
               OLD.task_plan_count IS NULL AND NEW.task_plan_count IS NOT NULL AND
               current_setting('lab.owner_run_id',true)=NEW.run_id::text AND
               (to_jsonb(NEW) - ARRAY[
                   'task_plan_sha256','task_plan_count','updated_at'
               ]) IS NOT DISTINCT FROM
               (to_jsonb(OLD) - ARRAY[
                   'task_plan_sha256','task_plan_count','updated_at'
               ]) THEN
                PERFORM lab.assert_director_generation_identity(
                    NEW.run_id,
                    nullif(current_setting('lab.owner_generation',true),'')::integer,
                    nullif(current_setting('lab.owner_invocation_id',true),''),
                    nullif(current_setting('lab.owner_execution_sha256',true),'')
                );
                RETURN NEW;
            END IF;
            PERFORM lab.assert_director_owner_context(OLD.run_id);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER director_run_update_statement_guard BEFORE UPDATE ON lab.runs "
        "FOR EACH STATEMENT EXECUTE FUNCTION lab.guard_director_run_update_statement()"
    )
    op.execute(
        "CREATE TRIGGER director_run_update_guard BEFORE UPDATE ON lab.runs "
        "FOR EACH ROW EXECUTE FUNCTION lab.guard_director_run_update()"
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_scorer_owned_statement()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE run_target uuid; generation_target integer; execution_target text;
            director_generation integer; director_invocation text;
        BEGIN
            IF session_user IN ('swapp_lab_director','swapp_lab_planner') THEN
                BEGIN
                    run_target := nullif(current_setting('lab.owner_run_id',true),'')::uuid;
                    director_generation := nullif(
                        current_setting('lab.owner_generation',true),'')::integer;
                EXCEPTION WHEN OTHERS THEN
                    RAISE EXCEPTION 'Director-owned Scorer mutation context is malformed';
                END;
                director_invocation := nullif(
                    current_setting('lab.owner_invocation_id',true),'');
                execution_target := nullif(
                    current_setting('lab.owner_execution_sha256',true),'');
                IF run_target IS NULL OR director_generation IS NULL OR
                   director_invocation IS NULL OR execution_target IS NULL THEN
                    RAISE EXCEPTION 'Director-owned Scorer mutation has no captured owner';
                END IF;
                IF TG_TABLE_NAME IN ('score_jobs','task_terminal_outcomes',
                                     'task_completions') AND
                   (SELECT state FROM lab.runs WHERE run_id=run_target)='stop_requested' THEN
                    PERFORM lab.assert_director_generation_identity(
                        run_target,director_generation,director_invocation,execution_target
                    );
                ELSE
                    PERFORM lab.assert_director_owner_context(run_target);
                END IF;
                RETURN NULL;
            ELSIF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'Scorer mutation requires the Scorer role';
            END IF;
            BEGIN
                run_target := nullif(current_setting('lab.scorer_run_id',true),'')::uuid;
                generation_target := nullif(
                    current_setting('lab.scorer_generation',true),'')::integer;
                execution_target := nullif(
                    current_setting('lab.scorer_execution_sha256',true),'');
            EXCEPTION WHEN OTHERS THEN
                RAISE EXCEPTION 'Scorer execution context is malformed';
            END;
            IF run_target IS NULL THEN
                run_target := nullif(
                    current_setting('lab.scorer_baseline_empty_stop_run_id',true),''
                )::uuid;
                IF run_target IS NULL OR TG_TABLE_SCHEMA <> 'lab' OR
                   TG_TABLE_NAME <> 'reports' THEN
                    RAISE EXCEPTION 'Scorer mutation has no captured run identity';
                END IF;
                PERFORM lab.assert_scorer_empty_baseline_stop(run_target);
                RETURN NULL;
            END IF;
            IF generation_target IS NULL OR execution_target IS NULL THEN
                RAISE EXCEPTION 'Scorer mutation has no captured generation';
            END IF;
            IF current_setting('lab.scorer_stop_closure',true)='true' THEN
                PERFORM lab.assert_scorer_run_stop_execution(
                    run_target,generation_target,execution_target
                );
            ELSE
                PERFORM lab.assert_scorer_run_execution(
                    run_target,generation_target,execution_target
                );
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_scorer_owned_row()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE run_target uuid; generation_target integer; execution_target text;
            is_stop boolean; director_generation integer; director_invocation text;
        BEGIN
            IF session_user IN ('swapp_lab_director','swapp_lab_planner') THEN
                BEGIN
                    run_target := nullif(current_setting('lab.owner_run_id',true),'')::uuid;
                    director_generation := nullif(
                        current_setting('lab.owner_generation',true),'')::integer;
                EXCEPTION WHEN OTHERS THEN
                    RAISE EXCEPTION 'Director-owned Scorer row context is malformed';
                END;
                director_invocation := nullif(
                    current_setting('lab.owner_invocation_id',true),'');
                execution_target := nullif(
                    current_setting('lab.owner_execution_sha256',true),'');
                IF run_target IS NULL OR director_generation IS NULL OR
                   director_invocation IS NULL OR execution_target IS NULL OR
                   NEW.run_id IS DISTINCT FROM run_target THEN
                    RAISE EXCEPTION 'Director-owned Scorer row is not bound to its owner';
                END IF;
                IF TG_TABLE_SCHEMA='scorer' AND TG_TABLE_NAME='score_jobs' THEN
                    IF NEW.admitted_generation IS DISTINCT FROM director_generation OR
                       NEW.execution_sha256 IS DISTINCT FROM execution_target THEN
                        RAISE EXCEPTION 'new score job is not bound to its admitting generation';
                    END IF;
                    IF TG_OP='INSERT' AND
                       (SELECT state FROM lab.runs WHERE run_id=run_target)='stop_requested' THEN
                        RAISE EXCEPTION 'Planner cannot enqueue a score job after stop';
                    END IF;
                    IF TG_OP='UPDATE' AND NOT (
                       OLD.state='queued' AND NEW.state='cancelled' AND
                       (SELECT state FROM lab.runs WHERE run_id=run_target)='stop_requested' AND
                       OLD.admitted_generation IS NOT DISTINCT FROM NEW.admitted_generation AND
                       OLD.execution_sha256 IS NOT DISTINCT FROM NEW.execution_sha256 AND
                       NEW.claimed_by IS NULL AND NEW.lease_until IS NULL AND
                       NEW.error_code='cancelled' AND
                       (to_jsonb(NEW) - ARRAY[
                           'state','claimed_by','lease_until','claim_unit',
                           'claim_invocation_id','error_code','updated_at'
                       ]) IS NOT DISTINCT FROM
                       (to_jsonb(OLD) - ARRAY[
                           'state','claimed_by','lease_until','claim_unit',
                           'claim_invocation_id','error_code','updated_at'
                       ])
                    ) THEN
                        RAISE EXCEPTION 'Planner score-job write is not a queued stop cancellation';
                    END IF;
                ELSIF TG_TABLE_SCHEMA='scorer' AND
                      TG_TABLE_NAME='task_terminal_outcomes' THEN
                    IF NEW.admitted_generation IS DISTINCT FROM director_generation OR
                       NEW.execution_sha256 IS DISTINCT FROM execution_target OR
                       ((SELECT state FROM lab.runs WHERE run_id=run_target)='stop_requested' AND
                        NEW.outcome_code IS DISTINCT FROM 'cancelled') THEN
                        RAISE EXCEPTION 'Planner outcome is not bound to its owner generation';
                    END IF;
                ELSIF TG_TABLE_SCHEMA='scorer' AND
                      TG_TABLE_NAME='task_completions' THEN
                    IF ((SELECT state FROM lab.runs WHERE run_id=run_target)='stop_requested' AND
                        NEW.completion_kind IS DISTINCT FROM 'terminal') OR NOT EXISTS (
                        SELECT 1 FROM scorer.run_tasks AS task
                         WHERE task.run_id=NEW.run_id
                           AND task.experiment_id=NEW.experiment_id
                           AND task.evaluation_kind=NEW.evaluation_kind
                           AND task.task_id=NEW.task_id AND task.seed=NEW.seed
                    ) THEN
                        RAISE EXCEPTION 'Planner task completion is not a planned terminal cell';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'Director role cannot write Scorer table %.%',
                        TG_TABLE_SCHEMA,TG_TABLE_NAME;
                END IF;
                IF TG_OP='DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            ELSIF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'Scorer mutation requires the Scorer role';
            END IF;
            BEGIN
                generation_target := nullif(
                    current_setting('lab.scorer_generation',true),'')::integer;
                execution_target := nullif(
                    current_setting('lab.scorer_execution_sha256',true),'');
                run_target := nullif(current_setting('lab.scorer_run_id',true),'')::uuid;
                is_stop := current_setting('lab.scorer_stop_closure',true)='true';
            EXCEPTION WHEN OTHERS THEN
                RAISE EXCEPTION 'Scorer execution context is malformed';
            END;
            IF run_target IS NULL THEN
                run_target := nullif(
                    current_setting('lab.scorer_baseline_empty_stop_run_id',true),''
                )::uuid;
                IF TG_TABLE_SCHEMA='lab' AND TG_TABLE_NAME='reports' AND
                   run_target IS NOT NULL AND NEW.run_id IS NOT DISTINCT FROM run_target THEN
                    IF NEW.report_json->>'schema' IS DISTINCT FROM 'lab.baseline-report.v1' OR
                       NEW.report_json->>'run_id' IS DISTINCT FROM run_target::text OR
                       NEW.report_json->>'purpose' IS DISTINCT FROM 'baseline' OR
                       NEW.report_json->>'status' IS DISTINCT FROM 'stopped' OR
                       NEW.report_json ? 'admitted_generation' OR
                       NEW.report_json ? 'execution_sha256' THEN
                        RAISE EXCEPTION 'ownerless baseline stop report identity is invalid';
                    END IF;
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'Scorer row mutation has no captured run identity';
            END IF;
            IF generation_target IS NULL OR execution_target IS NULL OR
               NEW.run_id IS DISTINCT FROM run_target THEN
                RAISE EXCEPTION 'Scorer row is not bound to the captured run';
            END IF;
            IF TG_TABLE_SCHEMA='scorer' AND TG_TABLE_NAME='score_jobs' THEN
                IF NEW.admitted_generation IS DISTINCT FROM generation_target OR
                   NEW.execution_sha256 IS DISTINCT FROM execution_target OR
                   (TG_OP='UPDATE' AND (
                       OLD.admitted_generation IS DISTINCT FROM NEW.admitted_generation OR
                       OLD.execution_sha256 IS DISTINCT FROM NEW.execution_sha256 OR
                       NEW.job_id IS DISTINCT FROM OLD.job_id OR
                       NEW.run_id IS DISTINCT FROM OLD.run_id OR
                       NEW.experiment_id IS DISTINCT FROM OLD.experiment_id OR
                       NEW.evaluation_kind IS DISTINCT FROM OLD.evaluation_kind OR
                       NEW.task_id IS DISTINCT FROM OLD.task_id OR
                       NEW.seed IS DISTINCT FROM OLD.seed OR
                       NEW.candidate_sha256 IS DISTINCT FROM OLD.candidate_sha256 OR
                       NEW.artifact_sha256 IS DISTINCT FROM OLD.artifact_sha256
                   )) THEN
                    RAISE EXCEPTION 'score job is not bound to its admitted generation';
                END IF;
                IF TG_OP='INSERT' THEN
                    RAISE EXCEPTION 'Scorer cannot directly enqueue a new score job';
                END IF;
                IF is_stop THEN
                    IF current_setting('lab.scorer_job_transition',true)
                           IS DISTINCT FROM 'cancel' OR
                       current_setting('lab.scorer_job_id',true)
                           IS DISTINCT FROM OLD.job_id::text OR
                       current_setting('lab.scorer_claim_token',true)
                           IS DISTINCT FROM OLD.claimed_by OR
                       current_setting('lab.scorer_worker_invocation_id',true)
                           IS DISTINCT FROM OLD.claim_invocation_id OR
                       OLD.state IS DISTINCT FROM 'running' OR
                       NEW.state IS DISTINCT FROM 'cancelled' OR
                       NEW.claimed_by IS NOT NULL OR NEW.lease_until IS NOT NULL OR
                       NEW.claim_unit IS NOT NULL OR NEW.claim_invocation_id IS NOT NULL OR
                       NEW.error_code IS DISTINCT FROM 'cancelled' OR
                       (to_jsonb(NEW) - ARRAY[
                           'state','claimed_by','lease_until','claim_unit',
                           'claim_invocation_id','error_code','updated_at'
                       ]) IS DISTINCT FROM
                       (to_jsonb(OLD) - ARRAY[
                           'state','claimed_by','lease_until','claim_unit',
                           'claim_invocation_id','error_code','updated_at'
                       ]) THEN
                        RAISE EXCEPTION 'Scorer cancellation is not bound to its live claim';
                    END IF;
                ELSIF current_setting('lab.scorer_job_id',true) IS DISTINCT FROM OLD.job_id::text
                    OR current_setting('lab.scorer_job_transition',true) IS NULL THEN
                    RAISE EXCEPTION 'Scorer score-job update has no validated claim';
                ELSIF current_setting('lab.scorer_job_transition',true)='claim' THEN
                    IF NEW.state IS DISTINCT FROM 'running' OR NEW.claimed_by IS NULL OR
                       NEW.claim_invocation_id IS NULL OR NEW.lease_until <= clock_timestamp() OR
                       NEW.error_code IS NOT NULL OR
                       (OLD.error_code IS NOT NULL AND
                        OLD.error_code <> 'retryable_infrastructure') OR
                       NEW.attempt IS DISTINCT FROM OLD.attempt + 1 OR
                       (OLD.state <> 'queued' AND NOT (
                           OLD.state='running' AND OLD.lease_until IS NOT NULL AND
                           OLD.lease_until <= clock_timestamp()
                       )) OR
                       (to_jsonb(NEW) - ARRAY[
                           'state','attempt','claimed_by','lease_until','claim_unit',
                           'claim_invocation_id','error_code','updated_at'
                       ]) IS DISTINCT FROM
                       (to_jsonb(OLD) - ARRAY[
                           'state','attempt','claimed_by','lease_until','claim_unit',
                           'claim_invocation_id','error_code','updated_at'
                       ]) THEN
                        RAISE EXCEPTION 'Scorer claim transition is invalid';
                    END IF;
                ELSE
                    IF current_setting('lab.scorer_job_transition',true)
                       NOT IN ('worker','cancel') OR
                       current_setting('lab.scorer_claim_token',true)
                           IS DISTINCT FROM OLD.claimed_by OR
                       current_setting('lab.scorer_worker_invocation_id',true)
                           IS DISTINCT FROM OLD.claim_invocation_id OR
                       OLD.state IS DISTINCT FROM 'running' OR
                       NEW.state NOT IN ('queued','completed','failed') OR
                       (NEW.state='completed' AND NOT EXISTS (
                           SELECT 1 FROM scorer.task_scores AS score
                            WHERE score.score_job_id=OLD.job_id
                              AND score.run_id=OLD.run_id
                              AND score.experiment_id=OLD.experiment_id
                              AND score.evaluation_kind=OLD.evaluation_kind
                              AND score.task_id=OLD.task_id AND score.seed=OLD.seed
                       )) OR
                       NEW.attempt IS DISTINCT FROM OLD.attempt OR
                       (to_jsonb(NEW) - ARRAY[
                           'state','attempt','claimed_by','lease_until','claim_unit',
                           'claim_invocation_id','error_code','updated_at'
                       ]) IS DISTINCT FROM
                       (to_jsonb(OLD) - ARRAY[
                           'state','attempt','claimed_by','lease_until','claim_unit',
                           'claim_invocation_id','error_code','updated_at'
                       ]) THEN
                        RAISE EXCEPTION 'Scorer result transition is invalid for its worker claim';
                    END IF;
                END IF;
            ELSIF TG_TABLE_SCHEMA='scorer' AND TG_TABLE_NAME='task_scores' THEN
                IF is_stop OR NOT EXISTS (
                    SELECT 1 FROM scorer.score_jobs AS job
                     WHERE job.job_id=NEW.score_job_id AND job.run_id=run_target
                       AND job.admitted_generation=generation_target
                       AND job.execution_sha256=execution_target
                ) THEN
                    RAISE EXCEPTION 'task score is not bound to its admitted score job';
                END IF;
            ELSIF TG_TABLE_SCHEMA='scorer' AND TG_TABLE_NAME='task_terminal_outcomes' THEN
                IF NEW.admitted_generation IS DISTINCT FROM generation_target OR
                   NEW.execution_sha256 IS DISTINCT FROM execution_target OR
                   (is_stop AND NEW.outcome_code IS DISTINCT FROM 'cancelled') THEN
                    RAISE EXCEPTION 'terminal outcome is not bound to its admitted generation';
                END IF;
            ELSIF TG_TABLE_SCHEMA='scorer' AND TG_TABLE_NAME='task_completions' THEN
                IF is_stop AND NEW.completion_kind IS DISTINCT FROM 'terminal' THEN
                    RAISE EXCEPTION 'stop closure may only publish terminal completions';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM scorer.score_jobs AS job
                     WHERE job.run_id=NEW.run_id AND job.experiment_id=NEW.experiment_id
                       AND job.evaluation_kind=NEW.evaluation_kind AND job.task_id=NEW.task_id
                       AND job.seed=NEW.seed
                       AND job.admitted_generation=generation_target
                       AND job.execution_sha256=execution_target
                ) THEN
                    RAISE EXCEPTION 'task completion has no matching admitted task';
                END IF;
            ELSIF TG_TABLE_SCHEMA='lab' AND TG_TABLE_NAME='reports' THEN
                IF NEW.report_json->>'run_id' IS DISTINCT FROM run_target::text OR
                   NEW.report_json->'admitted_generation' IS DISTINCT FROM
                       to_jsonb(generation_target) OR
                   NEW.report_json->>'execution_sha256' IS DISTINCT FROM execution_target THEN
                    RAISE EXCEPTION 'Scorer report omits its admitted generation identity';
                END IF;
            ELSE
                RAISE EXCEPTION 'Scorer row has no generation binding for %.%',
                    TG_TABLE_SCHEMA,TG_TABLE_NAME;
            END IF;
            IF TG_OP='DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table, operation in (
        ("scorer.score_jobs", "INSERT OR UPDATE"),
        ("scorer.task_scores", "INSERT"),
        ("scorer.task_completions", "INSERT"),
        ("scorer.task_terminal_outcomes", "INSERT"),
        ("lab.reports", "INSERT"),
    ):
        trigger_name = table.replace(".", "_") + "_scorer_generation_fence"
        op.execute(
            f"CREATE TRIGGER {trigger_name} BEFORE {operation} ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION lab.guard_scorer_owned_statement()"
        )
        row_trigger = table.replace(".", "_") + "_scorer_generation_row_fence"
        op.execute(
            f"CREATE TRIGGER {row_trigger} BEFORE {operation} ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION lab.guard_scorer_owned_row()"
        )
    op.execute(
        """
        CREATE FUNCTION lab.request_director_run_stop(
            p_run_id uuid, p_owner_id text, p_origin text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE run_item lab.runs%ROWTYPE; admitted_generation integer;
            execution_digest text; deadline_value timestamptz; requested_wall integer;
            changed boolean := false;
        BEGIN
            IF session_user <> 'swapp_lab_director' OR p_run_id IS NULL OR
               p_owner_id IS NULL OR length(p_owner_id) NOT BETWEEN 1 AND 128 OR
               p_owner_id <> btrim(p_owner_id) OR p_origin IS NULL OR
               p_origin NOT IN ('local','aos') THEN
                RAISE EXCEPTION 'API run stop request is malformed';
            END IF;
            PERFORM lab.lock_run_plan(p_run_id);
            SELECT * INTO run_item FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
            IF NOT FOUND OR run_item.owner_id IS DISTINCT FROM p_owner_id OR
               run_item.origin IS DISTINCT FROM p_origin THEN
                RAISE EXCEPTION 'API principal does not own this run';
            END IF;
            IF run_item.state IN ('queued','running') THEN
                PERFORM set_config('lab.api_stop_run_id',p_run_id::text,true);
                UPDATE lab.runs SET state='stop_requested',stop_requested=true,
                    updated_at=clock_timestamp() WHERE run_id=p_run_id;
                INSERT INTO lab.run_events(event_id,run_id,event_type,event_json,created_at)
                VALUES (gen_random_uuid(),p_run_id,'run.stop_requested','{}'::jsonb,
                    clock_timestamp());
                run_item.state := 'stop_requested';
                run_item.stop_requested := true;
                changed := true;
            END IF;
            SELECT control.current_generation,contract.execution_sha256,contract.deadline_at
              INTO admitted_generation,execution_digest,deadline_value
              FROM lab.director_execution_control AS control
              JOIN lab.director_execution_contracts AS contract
                ON contract.run_id=control.run_id
             WHERE control.run_id=p_run_id;
            IF deadline_value IS NULL THEN
                BEGIN
                    requested_wall := (run_item.request_json #>> '{budget,wall_seconds}')::integer;
                EXCEPTION WHEN OTHERS THEN
                    requested_wall := NULL;
                END;
                IF requested_wall BETWEEN 1 AND 14400 THEN
                    deadline_value := run_item.created_at + make_interval(secs=>requested_wall);
                END IF;
            END IF;
            RETURN jsonb_build_object(
                'state',run_item.state,'changed',changed,
                'purpose',run_item.request_json->>'purpose',
                'admitted_generation',admitted_generation,
                'execution_sha256',execution_digest,
                'deadline_at',deadline_value
            );
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_director_owned_statement()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE owner_run uuid;
        BEGIN
            IF session_user='swapp_lab_migrator' THEN
                RETURN NULL;
            END IF;
            BEGIN
                owner_run := nullif(current_setting('lab.owner_run_id',true),'')::uuid;
            EXCEPTION WHEN OTHERS THEN
                RAISE EXCEPTION 'Director transaction is missing its captured owner';
            END;
            IF TG_TABLE_SCHEMA='lab' AND TG_TABLE_NAME IN (
                'experiments','experiment_records','trajectory_records'
            ) THEN
                BEGIN
                    PERFORM lab.assert_director_generation_identity(
                        owner_run,
                        nullif(current_setting('lab.owner_generation',true),'')::integer,
                        nullif(current_setting('lab.owner_invocation_id',true),''),
                        nullif(current_setting('lab.owner_execution_sha256',true),'')
                    );
                EXCEPTION WHEN OTHERS THEN
                    RAISE EXCEPTION 'Director transaction is not bound to the target generation';
                END;
            ELSE
                PERFORM lab.assert_director_owner_context(owner_run);
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    for table, operation in (
        ("lab.experiments", "INSERT OR UPDATE OR DELETE"),
        ("lab.experiment_records", "INSERT"),
        ("lab.trajectory_records", "INSERT"),
        ("lab.baseline_calibrations", "INSERT"),
        ("lab.holdout_run_end_intents", "INSERT"),
        ("lab.holdout_run_end_unavailable", "INSERT"),
        ("lab.holdout_run_end_fences", "INSERT"),
        ("scorer.run_tasks", "INSERT OR UPDATE OR DELETE"),
    ):
        trigger_name = table.replace(".", "_") + "_owner_fence"
        op.execute(
            f"CREATE TRIGGER {trigger_name} BEFORE {operation} ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION lab.guard_director_owned_statement()"
        )
    op.execute(
        """
        CREATE FUNCTION lab.guard_director_owned_row()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE target_run uuid; target_experiment text;
        BEGIN
            IF session_user='swapp_lab_migrator' THEN
                IF TG_OP='DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            IF (TG_TABLE_SCHEMA='lab' AND TG_TABLE_NAME IN (
                'experiments','baseline_calibrations','holdout_run_end_intents',
                'holdout_run_end_unavailable','holdout_run_end_fences'
            )) OR (TG_TABLE_SCHEMA='scorer' AND TG_TABLE_NAME IN (
                'run_tasks','holdout_reservations'
            )) THEN
                IF TG_OP='DELETE' THEN target_run := OLD.run_id;
                ELSE target_run := NEW.run_id; END IF;
            ELSIF TG_TABLE_SCHEMA='lab' AND TG_TABLE_NAME IN (
                'experiment_records','trajectory_records'
            ) THEN
                IF TG_OP='DELETE' THEN target_experiment := OLD.experiment_id;
                ELSE target_experiment := NEW.experiment_id; END IF;
                SELECT run_id INTO target_run FROM lab.experiments
                 WHERE experiment_id=target_experiment;
            ELSE
                RAISE EXCEPTION 'Director row fence has no run binding for %.%',
                    TG_TABLE_SCHEMA,TG_TABLE_NAME;
            END IF;
            IF target_run IS NULL THEN
                RAISE EXCEPTION 'Director row mutation has no target run';
            END IF;
            IF TG_TABLE_SCHEMA='lab' AND TG_TABLE_NAME IN (
                'experiments','experiment_records','trajectory_records'
            ) THEN
                PERFORM lab.assert_director_generation_identity(
                    target_run,
                    nullif(current_setting('lab.owner_generation',true),'')::integer,
                    nullif(current_setting('lab.owner_invocation_id',true),''),
                    nullif(current_setting('lab.owner_execution_sha256',true),'')
                );
                IF (SELECT state FROM lab.runs WHERE run_id=target_run)='stop_requested' THEN
                    IF TG_TABLE_NAME='experiments' THEN
                        IF TG_OP='DELETE' OR NEW.status IS DISTINCT FROM 'abandoned' THEN
                            RAISE EXCEPTION
                                'stopped owner may only abandon registered experiments';
                        END IF;
                    ELSIF TG_TABLE_NAME='experiment_records' THEN
                        IF NEW.experiment_json->>'status' IS DISTINCT FROM 'abandoned' THEN
                            RAISE EXCEPTION
                                'stopped owner may only publish abandoned experiment records';
                        END IF;
                    ELSIF TG_TABLE_NAME='trajectory_records' AND NOT EXISTS (
                        SELECT 1 FROM lab.experiments AS experiment
                         WHERE experiment.experiment_id=NEW.experiment_id
                           AND experiment.status='abandoned'
                    ) THEN
                        RAISE EXCEPTION 'stopped owner may only publish abandoned trajectories';
                    END IF;
                ELSE
                    PERFORM lab.assert_director_owner_context(target_run);
                END IF;
            ELSE
                PERFORM lab.assert_director_owner_context(target_run);
            END IF;
            IF TG_OP='DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table, operation in (
        ("lab.experiments", "INSERT OR UPDATE OR DELETE"),
        ("lab.experiment_records", "INSERT"),
        ("lab.trajectory_records", "INSERT"),
        ("lab.baseline_calibrations", "INSERT"),
        ("lab.holdout_run_end_intents", "INSERT"),
        ("lab.holdout_run_end_unavailable", "INSERT"),
        ("lab.holdout_run_end_fences", "INSERT"),
        ("scorer.run_tasks", "INSERT OR UPDATE OR DELETE"),
    ):
        trigger_name = table.replace(".", "_") + "_owner_row_fence"
        op.execute(
            f"CREATE TRIGGER {trigger_name} BEFORE {operation} ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION lab.guard_director_owned_row()"
        )
    op.execute(
        """
        CREATE FUNCTION lab.guard_director_checkpoint_insert()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        BEGIN
            IF NEW.event_type='director.checkpoint' THEN
                PERFORM lab.assert_director_owner_context(NEW.run_id);
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER director_checkpoint_owner_fence BEFORE INSERT ON lab.run_events "
        "FOR EACH ROW EXECUTE FUNCTION lab.guard_director_checkpoint_insert()"
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_immutable_director_execution_row()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        BEGIN
            IF TG_OP='DELETE' AND session_user='swapp_lab_migrator' AND pg_trigger_depth()>1 THEN
                RETURN OLD;
            END IF;
            IF TG_OP <> 'INSERT' OR
               session_user <> 'swapp_lab_director' OR
               current_setting('lab.initial_director_claim',true)
                   IS DISTINCT FROM NEW.run_id::text THEN
                RAISE EXCEPTION 'Director execution identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table in (
        "director_execution_contracts",
        "director_owner_generations",
        "director_execution_control",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE INSERT OR UPDATE OR DELETE "
            f"ON lab.{table} FOR EACH ROW EXECUTE FUNCTION "
            "lab.guard_immutable_director_execution_row()"
        )
    op.execute(
        """
        CREATE FUNCTION lab.guard_director_restart_request()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        BEGIN
            IF TG_OP <> 'INSERT' OR session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'restart request identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER director_restart_request_immutable BEFORE INSERT OR UPDATE OR DELETE "
        "ON lab.director_restart_requests FOR EACH ROW "
        "EXECUTE FUNCTION lab.guard_director_restart_request()"
    )
    claim_signature = (
        "lab.claim_initial_director_execution(uuid,text,jsonb,text,integer,integer,bigint,"
        "text,text,text,text)"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {claim_signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {claim_signature} TO swapp_lab_director")
    op.execute("REVOKE ALL ON FUNCTION lab.assert_director_owner_context(uuid) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.assert_director_owner_context(uuid) "
        "TO swapp_lab_director,swapp_lab_planner"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "lab.assert_director_generation_identity(uuid,integer,text,text) FROM PUBLIC"
    )
    op.execute("REVOKE ALL ON FUNCTION lab.assert_planner_job_stop_execution(uuid) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.assert_planner_job_stop_execution(uuid) TO swapp_lab_planner"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "lab.assert_director_generation_identity(uuid,integer,text,text) "
        "TO swapp_lab_director,swapp_lab_planner"
    )
    # Keep the 0024 SQL bodies private and expose ordered, owner-checked wrappers.
    op.execute(
        "ALTER FUNCTION lab.register_baseline_operation(uuid,jsonb) "
        "RENAME TO register_baseline_operation_0024_unfenced"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION lab.register_baseline_operation_0024_unfenced(uuid,jsonb) "
        "FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer"
    )
    op.execute(
        """
        CREATE FUNCTION lab.register_baseline_operation(p_run_id uuid,p_budget jsonb)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        BEGIN
            IF session_user <> 'swapp_lab_director' OR p_run_id IS NULL THEN
                RAISE EXCEPTION 'baseline operation registration requires Director identity';
            END IF;
            PERFORM lab.assert_director_owner_context(p_run_id);
            RETURN lab.register_baseline_operation_0024_unfenced(p_run_id,p_budget);
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION lab.register_baseline_operation(uuid,jsonb) FROM PUBLIC")
    op.execute(
        "ALTER FUNCTION lab.prepare_unstarted_baseline_stop(uuid,text) "
        "RENAME TO prepare_unstarted_baseline_stop_0024_unfenced"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION lab.prepare_unstarted_baseline_stop_0024_unfenced(uuid,text) "
        "FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer"
    )
    op.execute(
        """
        CREATE FUNCTION lab.prepare_unstarted_baseline_stop(p_run_id uuid,p_payload_sha256 text)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        BEGIN
            IF session_user <> 'swapp_lab_scorer' OR p_run_id IS NULL THEN
                RAISE EXCEPTION 'unstarted baseline stop requires Scorer identity';
            END IF;
            PERFORM lab.assert_scorer_empty_baseline_stop(p_run_id);
            RETURN lab.prepare_unstarted_baseline_stop_0024_unfenced(
                p_run_id,p_payload_sha256
            );
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION lab.prepare_unstarted_baseline_stop(uuid,text) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION lab.request_director_run_stop(uuid,text,text) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.request_director_run_stop(uuid,text,text) "
        "TO swapp_lab_director"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.register_baseline_operation(uuid,jsonb) "
        "TO swapp_lab_director"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.prepare_unstarted_baseline_stop(uuid,text) "
        "TO swapp_lab_scorer"
    )
    for signature in (
        "lab.assert_scorer_run_execution(uuid,integer,text)",
        "lab.assert_scorer_run_stop_execution(uuid,integer,text)",
        "lab.assert_scorer_job_execution(uuid,text,text)",
        "lab.assert_scorer_job_stop_execution(uuid,text,text)",
        "lab.assert_scorer_empty_baseline_stop(uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO swapp_lab_scorer")
    op.execute("REVOKE ALL ON FUNCTION lab.guard_director_run_update_statement() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION lab.guard_director_run_update() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION lab.guard_scorer_owned_statement() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION lab.guard_scorer_owned_row() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION lab.guard_director_execution_claim() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION lab.guard_director_owned_statement() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION lab.guard_director_checkpoint_insert() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION lab.guard_immutable_director_execution_row() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION lab.guard_director_restart_request() FROM PUBLIC")
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "lab.close_director_run_if_owned(uuid,integer,text,text,text,text,text) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "lab.close_director_run_if_owned(uuid,integer,text,text,text,text,text) "
        "TO swapp_lab_director"
    )
    op.execute(
        "GRANT SELECT ON lab.director_execution_contracts,lab.director_owner_generations,"
        "lab.director_execution_control,lab.director_restart_requests TO swapp_lab_director"
    )


def downgrade() -> None:
    """Remove initial-generation execution ownership."""
    for table in (
        "director_restart_requests",
        "director_execution_control",
        "director_owner_generations",
        "director_execution_contracts",
    ):
        op.execute(
            f"DROP TRIGGER {table}_immutable ON lab.{table}"
        ) if table != "director_restart_requests" else op.execute(
            "DROP TRIGGER director_restart_request_immutable ON lab.director_restart_requests"
        )
    op.execute("DROP TRIGGER director_execution_claim_guard ON lab.runs")
    op.execute("DROP TRIGGER director_run_update_guard ON lab.runs")
    op.execute("DROP TRIGGER director_run_update_statement_guard ON lab.runs")
    op.execute("DROP TRIGGER director_checkpoint_owner_fence ON lab.run_events")
    for table, operation in (
        ("scorer.score_jobs", "scorer_generation_fence"),
        ("scorer.task_scores", "scorer_generation_fence"),
        ("scorer.task_completions", "scorer_generation_fence"),
        ("scorer.task_terminal_outcomes", "scorer_generation_fence"),
        ("lab.reports", "scorer_generation_fence"),
    ):
        op.execute(f"DROP TRIGGER {table.replace('.', '_')}_{operation} ON {table}")
        op.execute(f"DROP TRIGGER {table.replace('.', '_')}_scorer_generation_row_fence ON {table}")
    for table in (
        "lab.experiments",
        "lab.experiment_records",
        "lab.trajectory_records",
        "lab.baseline_calibrations",
        "lab.holdout_run_end_intents",
        "lab.holdout_run_end_unavailable",
        "lab.holdout_run_end_fences",
        "scorer.run_tasks",
    ):
        trigger_name = table.replace(".", "_") + "_owner_fence"
        op.execute(f"DROP TRIGGER {trigger_name} ON {table}")
        row_trigger_name = table.replace(".", "_") + "_owner_row_fence"
        op.execute(f"DROP TRIGGER {row_trigger_name} ON {table}")
    op.execute("DROP FUNCTION lab.request_director_run_stop(uuid,text,text)")
    op.execute("DROP FUNCTION lab.register_baseline_operation(uuid,jsonb)")
    op.execute(
        "ALTER FUNCTION lab.register_baseline_operation_0024_unfenced(uuid,jsonb) "
        "RENAME TO register_baseline_operation"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.register_baseline_operation(uuid,jsonb) "
        "TO swapp_lab_director"
    )
    op.execute("DROP FUNCTION lab.prepare_unstarted_baseline_stop(uuid,text)")
    op.execute(
        "ALTER FUNCTION lab.prepare_unstarted_baseline_stop_0024_unfenced(uuid,text) "
        "RENAME TO prepare_unstarted_baseline_stop"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.prepare_unstarted_baseline_stop(uuid,text) "
        "TO swapp_lab_scorer"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION lab.lock_run_plan(p_run_id uuid)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE lock_key bigint;
        BEGIN
            IF session_user NOT IN ('swapp_lab_director','swapp_lab_planner') THEN
                RAISE EXCEPTION 'run lifecycle lock requires Director or Planner role';
            END IF;
            lock_key := ('x' || substr(encode(sha256(uuid_send(p_run_id)), 'hex'), 1, 16))
                ::bit(64)::bigint;
            PERFORM pg_advisory_xact_lock(lock_key);
        END;
        $$
        """
    )
    op.execute("DROP FUNCTION lab.guard_director_restart_request()")
    op.execute("DROP FUNCTION lab.guard_director_checkpoint_insert()")
    op.execute("DROP FUNCTION lab.guard_director_owned_statement()")
    op.execute("DROP FUNCTION lab.guard_director_owned_row()")
    op.execute("DROP FUNCTION lab.guard_director_run_update_statement()")
    op.execute("DROP FUNCTION lab.guard_director_run_update()")
    op.execute("DROP FUNCTION lab.guard_scorer_owned_statement()")
    op.execute("DROP FUNCTION lab.guard_scorer_owned_row()")
    op.execute("DROP FUNCTION lab.assert_scorer_empty_baseline_stop(uuid)")
    op.execute("DROP FUNCTION lab.guard_immutable_director_execution_row()")
    op.execute("DROP FUNCTION lab.guard_director_execution_claim()")
    op.execute(
        "DROP FUNCTION lab.close_director_run_if_owned(uuid,integer,text,text,text,text,text)"
    )
    op.execute("DROP FUNCTION lab.assert_planner_job_stop_execution(uuid)")
    op.execute("DROP FUNCTION lab.assert_scorer_job_stop_execution(uuid,text,text)")
    op.execute("DROP FUNCTION lab.assert_scorer_job_execution(uuid,text,text)")
    op.execute("DROP FUNCTION lab.assert_scorer_run_stop_execution(uuid,integer,text)")
    op.execute("DROP FUNCTION lab.assert_scorer_run_execution(uuid,integer,text)")
    op.execute("DROP FUNCTION lab.assert_director_generation_identity(uuid,integer,text,text)")
    op.execute("DROP FUNCTION lab.assert_director_owner_context(uuid)")
    claim_signature = (
        "lab.claim_initial_director_execution(uuid,text,jsonb,text,integer,integer,bigint,"
        "text,text,text,text)"
    )
    op.execute(f"DROP FUNCTION {claim_signature}")
    op.drop_table("director_restart_requests", schema="lab")
    op.drop_table("director_execution_control", schema="lab")
    op.drop_table("director_owner_generations", schema="lab")
    op.drop_table("director_execution_contracts", schema="lab")
    op.drop_constraint(
        "ck_terminal_execution_sha", "task_terminal_outcomes", schema="scorer", type_="check"
    )
    op.drop_constraint(
        "ck_terminal_execution_owner_pair",
        "task_terminal_outcomes",
        schema="scorer",
        type_="check",
    )
    op.drop_constraint("ck_score_jobs_execution_sha", "score_jobs", schema="scorer", type_="check")
    op.drop_constraint(
        "ck_score_jobs_execution_owner_pair", "score_jobs", schema="scorer", type_="check"
    )
    for table, column in (
        ("task_terminal_outcomes", "execution_sha256"),
        ("task_terminal_outcomes", "admitted_generation"),
        ("score_jobs", "execution_sha256"),
        ("score_jobs", "admitted_generation"),
    ):
        op.drop_column(table, column, schema="scorer")
