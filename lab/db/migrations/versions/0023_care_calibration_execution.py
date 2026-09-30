"""Add durable, no-refund cell admission and worker-generation fencing."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0023_care_calibration_execution"
down_revision = "0022_care_baseline_calibration"
branch_labels = None
depends_on = None

CELL_RESERVATION_SECONDS = 100


def upgrade() -> None:
    op.create_table(
        "care_baseline_calibration_control",
        sa.Column("calibration_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="ready"),
        sa.Column("reserved_wall_seconds", sa.Integer, nullable=False, server_default="0"),
        sa.Column("terminal_failure_code", sa.String(32)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["calibration_id"], ["scorer.care_baseline_calibration_jobs.calibration_id"]
        ),
        sa.CheckConstraint(
            "state in ('ready','running','complete','incomplete')",
            name="ck_care_calibration_control_state",
        ),
        sa.CheckConstraint(
            "reserved_wall_seconds >= 0", name="ck_care_calibration_reserved_seconds"
        ),
        schema="scorer",
    )
    op.create_table(
        "care_baseline_calibration_claims",
        sa.Column("claim_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("calibration_id", UUID(as_uuid=True), nullable=False),
        sa.Column("task_key", sa.String(32), nullable=False),
        sa.Column("algorithm", sa.String(32), nullable=False),
        sa.Column("seed", sa.Integer, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reserved_seconds", sa.Integer, nullable=False),
        sa.Column(
            "reserved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("worker_pid", sa.Integer),
        sa.Column("worker_start_ticks", sa.String(32)),
        sa.Column("worker_boot_id", sa.String(36)),
        sa.Column("worker_unit", sa.String(128)),
        sa.Column("worker_invocation_id", sa.String(32)),
        sa.Column("worker_cgroup", sa.String(512)),
        sa.Column("failure_code", sa.String(32)),
        sa.Column("bound_at", sa.DateTime(timezone=True)),
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["calibration_id", "task_key"],
            [
                "scorer.care_baseline_calibration_tasks.calibration_id",
                "scorer.care_baseline_calibration_tasks.task_key",
            ],
        ),
        sa.UniqueConstraint(
            "calibration_id",
            "task_key",
            "algorithm",
            "seed",
            name="uq_care_calibration_cell_claim",
        ),
        sa.CheckConstraint("generation > 0", name="ck_care_calibration_claim_generation"),
        sa.CheckConstraint(
            "state in ('reserved','running','succeeded','failed')",
            name="ck_care_calibration_claim_state",
        ),
        sa.CheckConstraint(
            "reserved_seconds = 100", name="ck_care_calibration_claim_reserved_seconds"
        ),
        sa.CheckConstraint(
            "algorithm in ('robust_z','iforest','ecod_train_frozen')",
            name="ck_care_calibration_claim_algorithm",
        ),
        sa.CheckConstraint("seed in (0,1,2)", name="ck_care_calibration_claim_seed"),
        sa.CheckConstraint(
            "(state='reserved' and worker_pid is null and worker_unit is null) or "
            "(state<>'reserved' and worker_pid is not null and worker_unit is not null) or "
            "(state='failed' and worker_pid is null and worker_unit is null)",
            name="ck_care_calibration_claim_worker_shape",
        ),
        schema="scorer",
    )
    op.add_column(
        "care_baseline_calibration_cells",
        sa.Column("claim_id", UUID(as_uuid=True), nullable=True),
        schema="scorer",
    )
    op.add_column(
        "care_baseline_calibration_cells",
        sa.Column("claim_generation", sa.Integer, nullable=True),
        schema="scorer",
    )
    op.create_foreign_key(
        "fk_care_calibration_cell_claim",
        "care_baseline_calibration_cells",
        "care_baseline_calibration_claims",
        ["claim_id"],
        ["claim_id"],
        source_schema="scorer",
        referent_schema="scorer",
    )
    op.create_check_constraint(
        "ck_care_calibration_cell_claim_generation",
        "care_baseline_calibration_cells",
        "(claim_id is null and claim_generation is null) or "
        "(claim_id is not null and claim_generation > 0)",
        schema="scorer",
    )

    op.execute(
        "REVOKE ALL ON scorer.care_baseline_calibration_control, "
        "scorer.care_baseline_calibration_claims FROM PUBLIC, "
        "swapp_lab_director, swapp_lab_planner, swapp_lab_scorer"
    )
    op.execute(
        "GRANT SELECT, INSERT ON scorer.care_baseline_calibration_control TO swapp_lab_migrator"
    )
    op.execute(
        "GRANT SELECT ON scorer.care_baseline_calibration_control, "
        "scorer.care_baseline_calibration_claims TO swapp_lab_scorer"
    )
    op.execute("REVOKE INSERT ON scorer.care_baseline_calibration_cells FROM swapp_lab_scorer")

    op.execute(
        """
        CREATE FUNCTION lab.lock_care_calibration_execution(p_calibration_id uuid)
        RETURNS timestamptz LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE control_row scorer.care_baseline_calibration_control%ROWTYPE;
                job scorer.care_baseline_calibration_jobs%ROWTYPE;
        BEGIN
            IF session_user NOT IN ('swapp_lab_migrator','swapp_lab_scorer') THEN
                RAISE EXCEPTION 'CARE execution lock requires Migrator or Scorer';
            END IF;
            SELECT * INTO control_row FROM scorer.care_baseline_calibration_control c
             WHERE c.calibration_id=p_calibration_id FOR UPDATE;
            SELECT * INTO job FROM scorer.care_baseline_calibration_jobs j
             WHERE j.calibration_id=p_calibration_id FOR UPDATE;
            IF control_row.calibration_id IS NULL OR job.calibration_id IS NULL THEN
                RAISE EXCEPTION 'CARE calibration execution is missing';
            END IF;
            RETURN job.deadline_at;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION lab.lock_care_calibration_execution(uuid) FROM PUBLIC, "
        "swapp_lab_director, swapp_lab_planner"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.lock_care_calibration_execution(uuid) "
        "TO swapp_lab_migrator, swapp_lab_scorer"
    )

    op.execute(
        """
        CREATE FUNCTION lab.guard_care_calibration_task_claim()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE control_row scorer.care_baseline_calibration_control%ROWTYPE;
                job scorer.care_baseline_calibration_jobs%ROWTYPE;
                existing scorer.care_baseline_calibration_tasks%ROWTYPE;
                has_claim boolean;
                task_count integer;
        BEGIN
            SELECT * INTO control_row FROM scorer.care_baseline_calibration_control c
             WHERE c.calibration_id=NEW.calibration_id FOR UPDATE;
            SELECT * INTO job FROM scorer.care_baseline_calibration_jobs j
             WHERE j.calibration_id=NEW.calibration_id FOR UPDATE;
            SELECT * INTO existing FROM scorer.care_baseline_calibration_tasks t
             WHERE t.calibration_id=NEW.calibration_id AND t.task_key=NEW.task_key;
            SELECT EXISTS(SELECT 1 FROM scorer.care_baseline_calibration_claims c
                          WHERE c.calibration_id=NEW.calibration_id) INTO has_claim;
            SELECT count(*) INTO task_count FROM scorer.care_baseline_calibration_tasks t
             WHERE t.calibration_id=NEW.calibration_id;
            IF control_row.calibration_id IS NULL OR job.calibration_id IS NULL THEN
                RAISE EXCEPTION 'CARE calibration execution is missing';
            END IF;
            IF has_claim OR task_count >= 15 OR control_row.state <> 'ready'
               OR EXISTS(SELECT 1 FROM scorer.care_baseline_calibration_freezes f
                         WHERE f.calibration_id=NEW.calibration_id) THEN
                IF existing.task_key IS NULL OR to_jsonb(existing) IS DISTINCT FROM to_jsonb(NEW)
                THEN
                    RAISE EXCEPTION 'CARE task manifest is sealed by prior cell execution';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER care_calibration_task_claim_guard BEFORE INSERT "
        "ON scorer.care_baseline_calibration_tasks FOR EACH ROW "
        "EXECUTE FUNCTION lab.guard_care_calibration_task_claim()"
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_care_calibration_claim_cell()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE control_row scorer.care_baseline_calibration_control%ROWTYPE;
                job scorer.care_baseline_calibration_jobs%ROWTYPE;
                claim scorer.care_baseline_calibration_claims%ROWTYPE;
        BEGIN
            SELECT * INTO control_row FROM scorer.care_baseline_calibration_control c
             WHERE c.calibration_id=NEW.calibration_id FOR UPDATE;
            SELECT * INTO job FROM scorer.care_baseline_calibration_jobs j
             WHERE j.calibration_id=NEW.calibration_id FOR UPDATE;
            SELECT * INTO claim FROM scorer.care_baseline_calibration_claims c
             WHERE c.claim_id=NEW.claim_id FOR UPDATE;
            IF control_row.state <> 'running' OR claim.claim_id IS NULL
               OR claim.calibration_id IS DISTINCT FROM NEW.calibration_id
               OR claim.task_key IS DISTINCT FROM NEW.task_key
               OR claim.algorithm IS DISTINCT FROM NEW.algorithm
               OR claim.seed IS DISTINCT FROM NEW.seed
               OR claim.generation IS DISTINCT FROM NEW.claim_generation
               OR claim.state <> 'running'
               OR clock_timestamp() >= claim.deadline_at
               OR clock_timestamp() >= job.deadline_at THEN
                RAISE EXCEPTION 'CARE receipt insert lacks its live exact claim generation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER care_calibration_claim_cell_guard BEFORE INSERT "
        "ON scorer.care_baseline_calibration_cells FOR EACH ROW "
        "EXECUTE FUNCTION lab.guard_care_calibration_claim_cell()"
    )

    op.execute(
        """
        CREATE FUNCTION lab.reserve_care_baseline_cell(
            p_calibration_id uuid,
            p_claim_id uuid,
            p_task_key text,
            p_algorithm text,
            p_seed integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE control_row scorer.care_baseline_calibration_control%ROWTYPE;
                job scorer.care_baseline_calibration_jobs%ROWTYPE;
                existing scorer.care_baseline_calibration_claims%ROWTYPE;
                task_exists boolean;
                task_count integer;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'CARE cell reservation requires Scorer';
            END IF;
            IF p_calibration_id IS NULL OR p_claim_id IS NULL OR p_task_key IS NULL
               OR p_algorithm IS NULL OR p_seed IS NULL THEN
                RAISE EXCEPTION 'CARE cell reservation identity is incomplete';
            END IF;
            SELECT * INTO control_row FROM scorer.care_baseline_calibration_control
             WHERE calibration_id=p_calibration_id FOR UPDATE;
            SELECT * INTO job FROM scorer.care_baseline_calibration_jobs
             WHERE calibration_id=p_calibration_id FOR UPDATE;
            IF control_row.calibration_id IS NULL OR job.calibration_id IS NULL THEN
                RAISE EXCEPTION 'CARE calibration execution is missing';
            END IF;
            IF p_algorithm NOT IN ('robust_z','iforest','ecod_train_frozen')
               OR p_seed NOT IN (0,1,2) THEN
                RAISE EXCEPTION 'CARE cell key is outside the fixed measurement grid';
            END IF;
            SELECT EXISTS (SELECT 1 FROM scorer.care_baseline_calibration_tasks
                           WHERE calibration_id=p_calibration_id AND task_key=p_task_key)
              INTO task_exists;
            IF NOT task_exists THEN
                RAISE EXCEPTION 'CARE cell task is not in the immutable manifest';
            END IF;
            SELECT count(*) INTO task_count FROM scorer.care_baseline_calibration_tasks
             WHERE calibration_id=p_calibration_id;
            IF task_count <> 15 THEN
                RAISE EXCEPTION 'CARE cell claims require the exact 15-task manifest';
            END IF;
            SELECT * INTO existing FROM scorer.care_baseline_calibration_claims
             WHERE calibration_id=p_calibration_id AND task_key=p_task_key
               AND algorithm=p_algorithm AND seed=p_seed FOR UPDATE;
            IF FOUND THEN
                RETURN jsonb_build_object(
                    'claim_id',existing.claim_id,'generation',existing.generation,
                    'state',existing.state,'reserved_seconds',existing.reserved_seconds,
                    'reserved_at',existing.reserved_at,'deadline_at',existing.deadline_at,
                    'newly_created',false
                );
            END IF;
            IF control_row.state IN ('complete','incomplete')
               OR EXISTS (SELECT 1 FROM scorer.care_baseline_calibration_freezes
                          WHERE calibration_id=p_calibration_id)
               OR EXISTS (SELECT 1 FROM scorer.care_baseline_calibration_cells
                          WHERE calibration_id=p_calibration_id AND task_key=p_task_key
                            AND algorithm=p_algorithm AND seed=p_seed) THEN
                RAISE EXCEPTION 'CARE cell cannot be newly reserved in terminal execution';
            END IF;
            IF control_row.reserved_wall_seconds + 100 > job.wall_limit_seconds
               OR job.deadline_at < clock_timestamp() + interval '100 seconds' THEN
                UPDATE scorer.care_baseline_calibration_control
                   SET state='incomplete', terminal_failure_code='wall_budget_exhausted',
                       updated_at=clock_timestamp()
                 WHERE calibration_id=p_calibration_id;
                RETURN jsonb_build_object(
                    'state','incomplete','newly_created',false,
                    'reserved_wall_seconds',control_row.reserved_wall_seconds
                );
            END IF;
            UPDATE scorer.care_baseline_calibration_control
               SET state='running', reserved_wall_seconds=reserved_wall_seconds+100,
                   updated_at=clock_timestamp()
             WHERE calibration_id=p_calibration_id;
            INSERT INTO scorer.care_baseline_calibration_claims(
                claim_id,calibration_id,task_key,algorithm,seed,generation,state,reserved_seconds,
                deadline_at
            ) VALUES (
                p_claim_id,p_calibration_id,p_task_key,p_algorithm,p_seed,1,'reserved',100,
                least(job.deadline_at,clock_timestamp()+interval '100 seconds')
            );
            SELECT * INTO existing FROM scorer.care_baseline_calibration_claims
             WHERE claim_id=p_claim_id;
            RETURN jsonb_build_object(
                'claim_id',existing.claim_id,'generation',1,'state','reserved',
                'reserved_seconds',100,'reserved_at',existing.reserved_at,
                'deadline_at',existing.deadline_at,
                'newly_created',true
            );
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.bind_care_baseline_worker(
            p_claim_id uuid,
            p_generation integer,
            p_pid integer,
            p_start_ticks text,
            p_boot_id text,
            p_unit text,
            p_invocation_id text,
            p_cgroup text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE claim scorer.care_baseline_calibration_claims%ROWTYPE;
                control_row scorer.care_baseline_calibration_control%ROWTYPE;
                job scorer.care_baseline_calibration_jobs%ROWTYPE;
                v_calibration_id uuid;
                expected_unit text;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'CARE worker binding requires Scorer';
            END IF;
            IF p_claim_id IS NULL OR p_generation IS NULL OR p_pid IS NULL
               OR p_start_ticks IS NULL OR p_boot_id IS NULL OR p_unit IS NULL
               OR p_invocation_id IS NULL OR p_cgroup IS NULL THEN
                RAISE EXCEPTION 'CARE worker identity is incomplete';
            END IF;
            expected_unit := 'swapp-ai-scientist-scorer-' ||
                             replace(p_claim_id::text,'-','') || '.service';
            IF p_pid < 1 OR p_start_ticks !~ '^[0-9]+$'
               OR p_boot_id !~ '^[0-9a-fA-F-]{36}$'
               OR p_unit IS DISTINCT FROM expected_unit
               OR p_invocation_id !~ '^[0-9a-f]{32}$'
               OR p_cgroup IS NULL OR left(p_cgroup,1) <> '/'
               OR position('..' in p_cgroup) > 0
               OR right(p_cgroup,length('/' || p_unit)) <> '/' || p_unit THEN
                RAISE EXCEPTION 'CARE worker identity is malformed';
            END IF;
            SELECT c.calibration_id INTO v_calibration_id
              FROM scorer.care_baseline_calibration_claims c WHERE c.claim_id=p_claim_id;
            IF v_calibration_id IS NULL THEN
                RAISE EXCEPTION 'CARE worker claim is missing';
            END IF;
            SELECT * INTO control_row FROM scorer.care_baseline_calibration_control
             WHERE scorer.care_baseline_calibration_control.calibration_id=v_calibration_id
             FOR UPDATE;
            SELECT * INTO job FROM scorer.care_baseline_calibration_jobs
             WHERE scorer.care_baseline_calibration_jobs.calibration_id=v_calibration_id
             FOR UPDATE;
            SELECT * INTO claim FROM scorer.care_baseline_calibration_claims
             WHERE claim_id=p_claim_id FOR UPDATE;
            IF claim.claim_id IS NULL OR claim.generation IS DISTINCT FROM p_generation THEN
                RAISE EXCEPTION 'CARE worker claim generation is stale';
            END IF;
            IF claim.state='running' THEN
                IF ROW(claim.worker_pid,claim.worker_start_ticks,claim.worker_boot_id,
                       claim.worker_unit,claim.worker_invocation_id,claim.worker_cgroup)
                   IS DISTINCT FROM ROW(p_pid,p_start_ticks,p_boot_id,p_unit,
                                        p_invocation_id,p_cgroup) THEN
                    RAISE EXCEPTION 'CARE worker generation is already bound elsewhere';
                END IF;
                RETURN jsonb_build_object('claim_id',p_claim_id,'generation',p_generation,
                                          'state','running','newly_bound',false);
            END IF;
            IF claim.state <> 'reserved' THEN
                RAISE EXCEPTION 'CARE worker claim is terminal';
            END IF;
            IF control_row.state NOT IN ('running')
               OR clock_timestamp() >= claim.deadline_at
               OR clock_timestamp() >= job.deadline_at THEN
                RAISE EXCEPTION 'CARE worker claim deadline is exhausted';
            END IF;
            UPDATE scorer.care_baseline_calibration_claims
               SET state='running',worker_pid=p_pid,worker_start_ticks=p_start_ticks,
                   worker_boot_id=p_boot_id,worker_unit=p_unit,
                   worker_invocation_id=p_invocation_id,worker_cgroup=p_cgroup,
                   bound_at=clock_timestamp()
             WHERE claim_id=p_claim_id;
            RETURN jsonb_build_object('claim_id',p_claim_id,'generation',p_generation,
                                      'state','running','newly_bound',true);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.fail_care_baseline_cell(
            p_claim_id uuid,
            p_generation integer,
            p_failure_code text,
            p_worker_invocation_id text DEFAULT NULL
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE claim scorer.care_baseline_calibration_claims%ROWTYPE;
                control_row scorer.care_baseline_calibration_control%ROWTYPE;
                job scorer.care_baseline_calibration_jobs%ROWTYPE;
                v_calibration_id uuid;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'CARE cell failure publication requires Scorer';
            END IF;
            IF p_claim_id IS NULL OR p_generation IS NULL OR p_failure_code IS NULL
               OR p_failure_code NOT IN (
                'fit_rejected','score_rejected','worker_timeout','worker_crash',
                'deadline_exhausted','worker_start_failure'
            ) THEN
                RAISE EXCEPTION 'CARE cell failure code is not allowed';
            END IF;
            SELECT c.calibration_id INTO v_calibration_id
              FROM scorer.care_baseline_calibration_claims c WHERE c.claim_id=p_claim_id;
            IF v_calibration_id IS NULL THEN
                RAISE EXCEPTION 'CARE failure claim is missing';
            END IF;
            SELECT * INTO control_row FROM scorer.care_baseline_calibration_control c
             WHERE c.calibration_id=v_calibration_id FOR UPDATE;
            SELECT * INTO job FROM scorer.care_baseline_calibration_jobs j
             WHERE j.calibration_id=v_calibration_id FOR UPDATE;
            SELECT * INTO claim FROM scorer.care_baseline_calibration_claims
             WHERE claim_id=p_claim_id FOR UPDATE;
            IF claim.claim_id IS NULL OR claim.generation IS DISTINCT FROM p_generation THEN
                RAISE EXCEPTION 'CARE failure claim generation is stale';
            END IF;
            IF claim.state='failed' AND claim.failure_code=p_failure_code THEN
                RETURN jsonb_build_object('claim_id',p_claim_id,'generation',p_generation,
                                          'state','failed','failure_code',p_failure_code);
            END IF;
            IF claim.state NOT IN ('reserved','running')
               OR (claim.state='running'
                   AND claim.worker_invocation_id IS DISTINCT FROM p_worker_invocation_id) THEN
                RAISE EXCEPTION 'CARE failure does not match the active worker generation';
            END IF;
            UPDATE scorer.care_baseline_calibration_claims
               SET state='failed',failure_code=p_failure_code,terminal_at=clock_timestamp()
             WHERE claim_id=p_claim_id;
            UPDATE scorer.care_baseline_calibration_control c
               SET state='incomplete',terminal_failure_code=p_failure_code,
                   updated_at=clock_timestamp()
             WHERE c.calibration_id=claim.calibration_id;
            RETURN jsonb_build_object('claim_id',p_claim_id,'generation',p_generation,
                                      'state','failed','failure_code',p_failure_code);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.complete_care_baseline_cell(
            p_claim_id uuid,
            p_generation integer,
            p_worker_invocation_id text,
            p_receipt jsonb
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE claim scorer.care_baseline_calibration_claims%ROWTYPE;
                control_row scorer.care_baseline_calibration_control%ROWTYPE;
                job scorer.care_baseline_calibration_jobs%ROWTYPE;
                rec record;
                prior jsonb;
                expected jsonb;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'CARE receipt completion requires Scorer';
            END IF;
            IF p_claim_id IS NULL OR p_generation IS NULL
               OR p_worker_invocation_id IS NULL OR p_receipt IS NULL THEN
                RAISE EXCEPTION 'CARE completion identity is incomplete';
            END IF;
            SELECT c.calibration_id INTO rec FROM scorer.care_baseline_calibration_claims c
             WHERE c.claim_id=p_claim_id;
            IF rec.calibration_id IS NULL THEN
                RAISE EXCEPTION 'CARE completion claim is missing';
            END IF;
            SELECT * INTO control_row FROM scorer.care_baseline_calibration_control c
             WHERE c.calibration_id=rec.calibration_id FOR UPDATE;
            SELECT * INTO job FROM scorer.care_baseline_calibration_jobs j
             WHERE j.calibration_id=rec.calibration_id FOR UPDATE;
            SELECT * INTO claim FROM scorer.care_baseline_calibration_claims c
             WHERE c.claim_id=p_claim_id FOR UPDATE;
            IF claim.claim_id IS NULL OR claim.generation IS DISTINCT FROM p_generation THEN
                RAISE EXCEPTION 'CARE completion claim generation is stale';
            END IF;
            IF p_receipt->>'calibration_id' IS DISTINCT FROM claim.calibration_id::text THEN
                RAISE EXCEPTION 'CARE completion calibration identity differs from claim';
            END IF;
            SELECT * INTO rec FROM jsonb_to_record(p_receipt) AS x(
                baseline_source_sha256 text,harness_sha256 text,image_sha256 text,
                profile_sha256 text,train_sha256 text,evaluation_sha256 text,
                labels_sha256 text,semantics_sha256 text,fit_context_sha256 text,
                fit_artifact_sha256 text,score_document_sha256 text,policy_json jsonb,
                raw_task_score double precision,auxiliary_metrics_json jsonb,
                fit_seconds double precision,score_seconds double precision
            );
            IF rec.baseline_source_sha256 IS NULL OR rec.harness_sha256 IS NULL
               OR rec.image_sha256 IS NULL OR rec.profile_sha256 IS NULL
               OR rec.train_sha256 IS NULL OR rec.evaluation_sha256 IS NULL
               OR rec.labels_sha256 IS NULL OR rec.semantics_sha256 IS NULL
               OR rec.fit_context_sha256 IS NULL OR rec.fit_artifact_sha256 IS NULL
               OR rec.score_document_sha256 IS NULL OR rec.policy_json IS NULL
               OR rec.auxiliary_metrics_json IS NULL
               OR rec.raw_task_score IS NULL OR rec.raw_task_score < 0
               OR rec.raw_task_score > 1 OR rec.fit_seconds IS NULL OR rec.fit_seconds < 0
               OR rec.score_seconds IS NULL OR rec.score_seconds < 0
               OR rec.raw_task_score::text IN ('NaN','Infinity','-Infinity')
               OR rec.fit_seconds::text IN ('NaN','Infinity','-Infinity')
               OR rec.score_seconds::text IN ('NaN','Infinity','-Infinity') THEN
                RAISE EXCEPTION 'CARE completion receipt is malformed';
            END IF;
            expected := jsonb_build_object(
                'calibration_id',claim.calibration_id,'task_key',claim.task_key,
                'algorithm',claim.algorithm,'seed',claim.seed,'claim_id',claim.claim_id,
                'claim_generation',claim.generation,
                'baseline_source_sha256',rec.baseline_source_sha256,
                'harness_sha256',rec.harness_sha256,'image_sha256',rec.image_sha256,
                'profile_sha256',rec.profile_sha256,'train_sha256',rec.train_sha256,
                'evaluation_sha256',rec.evaluation_sha256,'labels_sha256',rec.labels_sha256,
                'semantics_sha256',rec.semantics_sha256,
                'fit_context_sha256',rec.fit_context_sha256,
                'fit_artifact_sha256',rec.fit_artifact_sha256,
                'score_document_sha256',rec.score_document_sha256,'policy_json',rec.policy_json,
                'raw_task_score',rec.raw_task_score,
                'auxiliary_metrics_json',rec.auxiliary_metrics_json,
                'fit_seconds',rec.fit_seconds,'score_seconds',rec.score_seconds
            );
            SELECT to_jsonb(c)-'created_at' INTO prior
              FROM scorer.care_baseline_calibration_cells c
             WHERE c.calibration_id=claim.calibration_id AND c.task_key=claim.task_key
               AND c.algorithm=claim.algorithm AND c.seed=claim.seed;
            IF prior IS NOT NULL THEN
                IF prior IS DISTINCT FROM expected OR claim.state <> 'succeeded'
                   OR claim.worker_invocation_id IS DISTINCT FROM p_worker_invocation_id THEN
                    RAISE EXCEPTION 'CARE cell conflicts with immutable claim receipt';
                END IF;
                RETURN jsonb_build_object('claim_id',p_claim_id,'generation',p_generation,
                                          'state','succeeded','newly_completed',false);
            END IF;
            IF claim.state <> 'running'
               OR claim.worker_invocation_id IS DISTINCT FROM p_worker_invocation_id
               OR control_row.state <> 'running'
               OR clock_timestamp() >= claim.deadline_at
               OR clock_timestamp() >= job.deadline_at THEN
                RAISE EXCEPTION 'CARE worker generation is not eligible to commit';
            END IF;
            INSERT INTO scorer.care_baseline_calibration_cells(
                calibration_id,task_key,algorithm,seed,claim_id,claim_generation,
                baseline_source_sha256,harness_sha256,image_sha256,profile_sha256,
                train_sha256,evaluation_sha256,labels_sha256,semantics_sha256,
                fit_context_sha256,fit_artifact_sha256,score_document_sha256,policy_json,
                raw_task_score,auxiliary_metrics_json,fit_seconds,score_seconds
            ) VALUES (
                claim.calibration_id,claim.task_key,claim.algorithm,claim.seed,claim.claim_id,
                claim.generation,rec.baseline_source_sha256,rec.harness_sha256,rec.image_sha256,
                rec.profile_sha256,rec.train_sha256,rec.evaluation_sha256,rec.labels_sha256,
                rec.semantics_sha256,rec.fit_context_sha256,rec.fit_artifact_sha256,
                rec.score_document_sha256,rec.policy_json,rec.raw_task_score,
                rec.auxiliary_metrics_json,rec.fit_seconds,rec.score_seconds
            );
            UPDATE scorer.care_baseline_calibration_claims c
               SET state='succeeded',terminal_at=clock_timestamp()
             WHERE c.claim_id=p_claim_id AND c.generation=p_generation AND c.state='running';
            IF NOT FOUND THEN RAISE EXCEPTION 'CARE claim changed during completion'; END IF;
            RETURN jsonb_build_object('claim_id',p_claim_id,'generation',p_generation,
                                      'state','succeeded','newly_completed',true);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.finalize_care_baseline_calibration(
            p_calibration_id uuid,
            p_manifest_sha256 text,
            p_calibration_sha256 text,
            p_task_summaries jsonb
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE control_row scorer.care_baseline_calibration_control%ROWTYPE;
                job scorer.care_baseline_calibration_jobs%ROWTYPE;
                existing scorer.care_baseline_calibration_freezes%ROWTYPE;
                summary record;
                n_cells integer;
                n_tasks integer;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'CARE calibration freeze requires Scorer';
            END IF;
            SELECT * INTO control_row FROM scorer.care_baseline_calibration_control c
             WHERE c.calibration_id=p_calibration_id FOR UPDATE;
            SELECT * INTO job FROM scorer.care_baseline_calibration_jobs j
             WHERE j.calibration_id=p_calibration_id FOR UPDATE;
            IF control_row.calibration_id IS NULL OR job.calibration_id IS NULL THEN
                RAISE EXCEPTION 'CARE calibration execution is missing';
            END IF;
            SELECT * INTO existing FROM scorer.care_baseline_calibration_freezes f
             WHERE f.calibration_id=p_calibration_id;
            IF FOUND THEN
                IF existing.manifest_sha256 IS DISTINCT FROM p_manifest_sha256
                   OR existing.calibration_sha256 IS DISTINCT FROM p_calibration_sha256
                   OR existing.task_count <> 15 OR existing.cell_count <> 135
                   OR control_row.state <> 'complete' THEN
                    RAISE EXCEPTION 'CARE calibration freeze conflicts with completed job';
                END IF;
                RETURN jsonb_build_object('state','complete','newly_frozen',false,
                                          'cell_count',135);
            END IF;
            IF control_row.state <> 'running' OR clock_timestamp() >= job.deadline_at THEN
                RAISE EXCEPTION 'CARE calibration deadline or execution state prevents freeze';
            END IF;
            SELECT count(*) INTO n_cells
              FROM scorer.care_baseline_calibration_cells c
              JOIN scorer.care_baseline_calibration_claims claim
                ON claim.claim_id=c.claim_id AND claim.generation=c.claim_generation
               AND claim.calibration_id=c.calibration_id AND claim.task_key=c.task_key
               AND claim.algorithm=c.algorithm AND claim.seed=c.seed
             WHERE c.calibration_id=p_calibration_id AND claim.state='succeeded';
            SELECT count(*) INTO n_tasks FROM scorer.care_baseline_calibration_tasks t
             WHERE t.calibration_id=p_calibration_id;
            IF n_cells <> 135 OR n_tasks <> 15 OR jsonb_typeof(p_task_summaries) <> 'array'
               OR jsonb_array_length(p_task_summaries) <> 15 THEN
                RAISE EXCEPTION 'CARE calibration grid is incomplete';
            END IF;
            FOR summary IN SELECT * FROM jsonb_to_recordset(p_task_summaries)
                AS x(task_key text,base_score double precision,reference_score double precision,
                     task_weight double precision)
            LOOP
                IF summary.task_weight IS DISTINCT FROM 1.0/15.0
                   OR summary.base_score IS NULL OR summary.reference_score IS NULL
                   OR summary.base_score < 0 OR summary.base_score > 1
                   OR summary.reference_score < 0 OR summary.reference_score > 1 THEN
                    RAISE EXCEPTION 'CARE calibration task summary is malformed';
                END IF;
                INSERT INTO scorer.care_baseline_calibration_task_summaries(
                    calibration_id,task_key,base_score,reference_score,task_weight
                ) VALUES (
                    p_calibration_id,summary.task_key,summary.base_score,
                    summary.reference_score,summary.task_weight
                );
            END LOOP;
            IF (SELECT count(*) FROM scorer.care_baseline_calibration_task_summaries s
                WHERE s.calibration_id=p_calibration_id) <> 15 THEN
                RAISE EXCEPTION 'CARE calibration summaries do not cover all tasks';
            END IF;
            INSERT INTO scorer.care_baseline_calibration_freezes(
                calibration_id,manifest_sha256,calibration_sha256,task_count,cell_count
            ) VALUES (p_calibration_id,p_manifest_sha256,p_calibration_sha256,15,135);
            UPDATE scorer.care_baseline_calibration_control c
               SET state='complete',terminal_failure_code=NULL,updated_at=clock_timestamp()
             WHERE c.calibration_id=p_calibration_id;
            RETURN jsonb_build_object('state','complete','newly_frozen',true,'cell_count',135);
        END;
        $$
        """
    )
    for signature in (
        "lab.reserve_care_baseline_cell(uuid,uuid,text,text,integer)",
        "lab.bind_care_baseline_worker(uuid,integer,integer,text,text,text,text,text)",
        "lab.fail_care_baseline_cell(uuid,integer,text,text)",
        "lab.complete_care_baseline_cell(uuid,integer,text,jsonb)",
        "lab.finalize_care_baseline_calibration(uuid,text,text,jsonb)",
    ):
        op.execute(
            f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, swapp_lab_director, swapp_lab_planner"
        )
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.reserve_care_baseline_cell(uuid,uuid,text,text,integer) "
        "TO swapp_lab_scorer"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "lab.bind_care_baseline_worker(uuid,integer,integer,text,text,text,text,text) "
        "TO swapp_lab_scorer"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.fail_care_baseline_cell(uuid,integer,text,text) "
        "TO swapp_lab_scorer"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.complete_care_baseline_cell(uuid,integer,text,jsonb) "
        "TO swapp_lab_scorer"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.finalize_care_baseline_calibration(uuid,text,text,jsonb) "
        "TO swapp_lab_scorer"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION lab.finalize_care_baseline_calibration(uuid,text,text,jsonb)")
    op.execute("DROP FUNCTION lab.complete_care_baseline_cell(uuid,integer,text,jsonb)")
    op.execute("DROP FUNCTION lab.lock_care_calibration_execution(uuid)")
    op.execute("DROP FUNCTION lab.fail_care_baseline_cell(uuid,integer,text,text)")
    op.execute(
        "DROP FUNCTION lab.bind_care_baseline_worker(uuid,integer,integer,text,text,text,text,text)"
    )
    op.execute("DROP FUNCTION lab.reserve_care_baseline_cell(uuid,uuid,text,text,integer)")
    op.execute(
        "DROP TRIGGER care_calibration_claim_cell_guard ON scorer.care_baseline_calibration_cells"
    )
    op.execute("DROP FUNCTION lab.guard_care_calibration_claim_cell()")
    op.execute(
        "DROP TRIGGER care_calibration_task_claim_guard ON scorer.care_baseline_calibration_tasks"
    )
    op.execute("DROP FUNCTION lab.guard_care_calibration_task_claim()")
    op.drop_constraint(
        "ck_care_calibration_cell_claim_generation",
        "care_baseline_calibration_cells",
        schema="scorer",
        type_="check",
    )
    op.drop_constraint(
        "fk_care_calibration_cell_claim",
        "care_baseline_calibration_cells",
        schema="scorer",
        type_="foreignkey",
    )
    op.drop_column("care_baseline_calibration_cells", "claim_generation", schema="scorer")
    op.drop_column("care_baseline_calibration_cells", "claim_id", schema="scorer")
    op.drop_table("care_baseline_calibration_claims", schema="scorer")
    op.drop_table("care_baseline_calibration_control", schema="scorer")
