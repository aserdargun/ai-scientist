"""Bind proposal records to an immutable, run-scoped baseline calibration."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_baseline_calibration"
down_revision = "0012_experiment_report_fence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Store one calibration receipt and bind experiment/trajectory JSON identity."""
    op.add_column("experiments", sa.Column("calibration_sha256", sa.String(64)), schema="lab")
    op.create_check_constraint(
        "ck_experiments_calibration_sha",
        "experiments",
        "calibration_sha256 is null or length(calibration_sha256) = 64",
        schema="lab",
    )
    op.create_table(
        "baseline_calibrations",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("suite_id", sa.String(128), nullable=False),
        sa.Column("suite_version", sa.Integer(), nullable=False),
        sa.Column("calibration_sha256", sa.String(64), nullable=False),
        sa.Column("blob_sha256", sa.String(64), nullable=False),
        sa.Column("task_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("suite_version > 0", name="ck_calibration_suite_version"),
        sa.CheckConstraint("length(calibration_sha256) = 64", name="ck_calibration_sha"),
        sa.CheckConstraint("length(blob_sha256) = 64", name="ck_calibration_blob_sha"),
        sa.CheckConstraint("task_count > 0", name="ck_calibration_task_count"),
        schema="lab",
    )
    op.execute("REVOKE ALL ON lab.baseline_calibrations FROM PUBLIC")
    op.execute(
        "REVOKE ALL ON lab.baseline_calibrations "
        "FROM swapp_lab_director, swapp_lab_planner, swapp_lab_scorer"
    )
    op.execute("GRANT DELETE ON lab.baseline_calibrations TO swapp_lab_migrator")

    op.execute(
        """
        CREATE FUNCTION lab.guard_experiment_calibration_identity()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE receipt lab.baseline_calibrations%ROWTYPE; requested text;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'experiment calibration identity requires the Director role';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF NEW.calibration_sha256 IS DISTINCT FROM OLD.calibration_sha256 THEN
                    RAISE EXCEPTION 'experiment calibration identity is immutable';
                END IF;
                RETURN NEW;
            END IF;
            SELECT * INTO receipt FROM lab.baseline_calibrations WHERE run_id = NEW.run_id;
            IF NEW.kind = 'baseline' THEN
                IF NEW.calibration_sha256 IS NOT NULL OR receipt.run_id IS NOT NULL THEN
                    RAISE EXCEPTION 'baseline identity is invalid after calibration freeze';
                END IF;
                RETURN NEW;
            END IF;
            requested := NEW.proposal_json->>'calibration_sha256';
            IF NEW.kind <> 'proposal' OR requested IS NULL OR requested !~ '^[0-9a-f]{64}$' OR
               receipt.run_id IS NULL OR receipt.calibration_sha256 <> requested THEN
                RAISE EXCEPTION 'proposal requires the frozen run calibration digest';
            END IF;
            NEW.calibration_sha256 := requested;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER aaa_experiment_calibration_identity
        BEFORE INSERT OR UPDATE ON lab.experiments
        FOR EACH ROW EXECUTE FUNCTION lab.guard_experiment_calibration_identity()
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_experiment_record_kind_identity()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE exp lab.experiments%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'experiment record identity requires the Director role';
            END IF;
            SELECT * INTO exp FROM lab.experiments WHERE experiment_id = NEW.experiment_id;
            IF NOT FOUND OR NEW.experiment_json->>'kind' IS DISTINCT FROM exp.kind OR
               NEW.experiment_json->'experiment_number' IS DISTINCT FROM
                   coalesce(to_jsonb(exp.experiment_number), 'null'::jsonb) OR
               NEW.experiment_json->>'baseline_name' IS DISTINCT FROM exp.baseline_name OR
               NEW.experiment_json->>'calibration_sha256' IS DISTINCT FROM
                   exp.calibration_sha256 THEN
                RAISE EXCEPTION 'experiment document kind identity differs from its registration';
            END IF;
            IF exp.kind = 'baseline' AND (
                NEW.experiment_json->'predicted_delta' IS DISTINCT FROM 'null'::jsonb OR
                NEW.experiment_json->'decision' IS DISTINCT FROM 'null'::jsonb
            ) THEN
                RAISE EXCEPTION 'baseline documents cannot invent proposal or Referee values';
            END IF;
            IF exp.kind = 'proposal' AND (
                NEW.experiment_json->'predicted_delta' IS NULL OR
                NEW.experiment_json->'predicted_delta' = 'null'::jsonb OR
                NEW.experiment_json->'decision' IS NULL OR
                NEW.experiment_json->'decision' = 'null'::jsonb
            ) THEN
                RAISE EXCEPTION 'proposal documents require their prediction and Referee result';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER aaa_experiment_record_kind_identity
        BEFORE INSERT ON lab.experiment_records
        FOR EACH ROW EXECUTE FUNCTION lab.guard_experiment_record_kind_identity()
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_trajectory_kind_identity()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE exp lab.experiments%ROWTYPE; rec lab.experiment_records%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'trajectory identity requires the Director role';
            END IF;
            SELECT * INTO exp FROM lab.experiments WHERE experiment_id = NEW.experiment_id;
            SELECT * INTO rec FROM lab.experiment_records WHERE experiment_id = NEW.experiment_id;
            IF exp.experiment_id IS NULL OR rec.experiment_id IS NULL OR
               NEW.trajectory_json->>'kind' IS DISTINCT FROM exp.kind OR
               NEW.trajectory_json->'experiment_number' IS DISTINCT FROM
                   coalesce(to_jsonb(exp.experiment_number), 'null'::jsonb) OR
               NEW.trajectory_json->>'baseline_name' IS DISTINCT FROM exp.baseline_name OR
               NEW.trajectory_json->>'calibration_sha256' IS DISTINCT FROM exp.calibration_sha256 OR
               NEW.trajectory_json->'outcome' IS DISTINCT FROM rec.experiment_json->'decision' THEN
                RAISE EXCEPTION 'trajectory identity differs from its experiment record';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER aaa_trajectory_kind_identity
        BEFORE INSERT ON lab.trajectory_records
        FOR EACH ROW EXECUTE FUNCTION lab.guard_trajectory_kind_identity()
        """
    )

    op.execute(
        """
        CREATE FUNCTION lab.register_baseline_calibration(
            p_run_id uuid, p_suite_id text, p_suite_version integer,
            p_calibration_sha256 text, p_blob_sha256 text, p_task_count integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE current_run lab.runs%ROWTYPE; existing lab.baseline_calibrations%ROWTYPE;
            baseline_count integer; planned_count bigint; scored_count bigint;
            exp lab.experiments%ROWTYPE; assigned bigint; completed bigint;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'baseline calibration requires the Director role';
            END IF;
            IF p_suite_id IS NULL OR length(p_suite_id) NOT BETWEEN 1 AND 128 OR
               p_suite_version IS NULL OR p_suite_version < 1 OR
               p_calibration_sha256 IS NULL OR p_calibration_sha256 !~ '^[0-9a-f]{64}$' OR
               p_blob_sha256 IS NULL OR p_blob_sha256 !~ '^[0-9a-f]{64}$' OR
               p_task_count IS NULL OR p_task_count < 1 THEN
                RAISE EXCEPTION 'baseline calibration receipt fields are invalid';
            END IF;
            PERFORM lab.lock_run_plan(p_run_id);
            SELECT * INTO current_run FROM lab.runs WHERE run_id = p_run_id FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'calibration run does not exist';
            END IF;
            SELECT * INTO existing FROM lab.baseline_calibrations
             WHERE run_id = p_run_id FOR UPDATE;
            IF FOUND THEN
                IF (existing.suite_id, existing.suite_version, existing.calibration_sha256,
                    existing.blob_sha256, existing.task_count) IS DISTINCT FROM
                   (p_suite_id, p_suite_version, p_calibration_sha256, p_blob_sha256,
                    p_task_count) THEN
                    RAISE EXCEPTION 'run already has a different frozen calibration';
                END IF;
                RETURN jsonb_build_object('status','already_registered',
                    'run_id',p_run_id,'calibration_sha256',existing.calibration_sha256,
                    'blob_sha256',existing.blob_sha256,'task_count',existing.task_count);
            END IF;
            IF current_run.state <> 'running' OR current_run.task_plan_sha256 IS NOT NULL THEN
                RAISE EXCEPTION 'new calibration requires a running unsealed run';
            END IF;
            IF p_calibration_sha256 <> p_blob_sha256 THEN
                RAISE EXCEPTION 'calibration receipt must bind the exact canonical blob digest';
            END IF;
            SELECT count(*) INTO baseline_count FROM lab.experiments
             WHERE run_id = p_run_id AND kind = 'baseline' AND status = 'scored';
            IF baseline_count <> 3 OR
               (SELECT count(DISTINCT baseline_name) FROM lab.experiments
                 WHERE run_id = p_run_id AND kind = 'baseline') <> 3 OR EXISTS (
                SELECT 1 FROM lab.experiments WHERE run_id = p_run_id AND kind = 'baseline'
                  AND baseline_name NOT IN ('robust_z','iforest','ecod_train_frozen')
            ) THEN
                RAISE EXCEPTION 'all three registered baseline experiments must be scored';
            END IF;
            SELECT count(*) INTO planned_count FROM scorer.run_tasks
             WHERE run_id = p_run_id AND evaluation_kind = 'baseline';
            SELECT count(*) INTO scored_count FROM scorer.task_scores
             WHERE run_id = p_run_id AND evaluation_kind = 'baseline';
            IF planned_count <> p_task_count::bigint * 9 OR scored_count <> planned_count OR
               EXISTS (SELECT 1 FROM scorer.run_tasks WHERE run_id = p_run_id
                        AND evaluation_kind = 'baseline' AND seed NOT IN (0,1,2)) OR
               EXISTS (SELECT 1 FROM scorer.run_tasks rt LEFT JOIN scorer.task_scores ts
                         USING (run_id,experiment_id,evaluation_kind,task_id,seed)
                        WHERE rt.run_id = p_run_id AND rt.evaluation_kind = 'baseline'
                          AND ts.run_id IS NULL) THEN
                RAISE EXCEPTION
                    'baseline task assignments must be algorithms×tasks×seeds 0..2';
            END IF;
            IF (SELECT count(DISTINCT (dataset_id,split_id,session_id,task_id))
                  FROM scorer.run_tasks WHERE run_id = p_run_id AND evaluation_kind = 'baseline')
               <> p_task_count THEN
                RAISE EXCEPTION 'baseline task identities differ from the calibration task count';
            END IF;
            FOR exp IN SELECT * FROM lab.experiments
                        WHERE run_id = p_run_id AND kind = 'baseline' LOOP
                PERFORM lab.experiment_record_receipt(exp.experiment_id);
                SELECT count(*) INTO assigned FROM scorer.run_tasks
                 WHERE run_id = p_run_id AND experiment_id = exp.experiment_id
                   AND evaluation_kind = 'baseline';
                SELECT count(*) INTO completed FROM scorer.task_scores
                 WHERE run_id = p_run_id AND experiment_id = exp.experiment_id
                   AND evaluation_kind = 'baseline';
                IF assigned <> p_task_count::bigint * 3 OR completed <> assigned THEN
                    RAISE EXCEPTION 'baseline task seed coverage is incomplete';
                END IF;
            END LOOP;
            INSERT INTO lab.baseline_calibrations(
                run_id,suite_id,suite_version,calibration_sha256,blob_sha256,task_count
            ) VALUES (
                p_run_id,p_suite_id,p_suite_version,p_calibration_sha256,p_blob_sha256,p_task_count
            );
            RETURN jsonb_build_object('status','registered','run_id',p_run_id,
                'calibration_sha256',p_calibration_sha256,'blob_sha256',p_blob_sha256,
                'task_count',p_task_count);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.baseline_calibration_receipt(p_run_id uuid)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE item lab.baseline_calibrations%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'calibration receipt requires the Director role';
            END IF;
            SELECT * INTO item FROM lab.baseline_calibrations WHERE run_id = p_run_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'run has no frozen baseline calibration'; END IF;
            RETURN jsonb_build_object('run_id',item.run_id,'suite_id',item.suite_id,
                'suite_version',item.suite_version,'calibration_sha256',item.calibration_sha256,
                'blob_sha256',item.blob_sha256,'task_count',item.task_count);
        END;
        $$
        """
    )
    for signature in (
        "lab.guard_experiment_calibration_identity()",
        "lab.guard_experiment_record_kind_identity()",
        "lab.guard_trajectory_kind_identity()",
        "lab.register_baseline_calibration(uuid,text,integer,text,text,integer)",
        "lab.baseline_calibration_receipt(uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "lab.register_baseline_calibration(uuid,text,integer,text,text,integer) "
        "TO swapp_lab_director"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.baseline_calibration_receipt(uuid) TO swapp_lab_director"
    )


def downgrade() -> None:
    """Remove calibration binding objects after active runs have been drained."""
    op.execute("DROP FUNCTION lab.baseline_calibration_receipt(uuid)")
    op.execute(
        "DROP FUNCTION lab.register_baseline_calibration(uuid,text,integer,text,text,integer)"
    )
    op.execute("DROP TRIGGER aaa_trajectory_kind_identity ON lab.trajectory_records")
    op.execute("DROP FUNCTION lab.guard_trajectory_kind_identity()")
    op.execute("DROP TRIGGER aaa_experiment_record_kind_identity ON lab.experiment_records")
    op.execute("DROP FUNCTION lab.guard_experiment_record_kind_identity()")
    op.execute("DROP TRIGGER aaa_experiment_calibration_identity ON lab.experiments")
    op.execute("DROP FUNCTION lab.guard_experiment_calibration_identity()")
    op.drop_table("baseline_calibrations", schema="lab")
    op.drop_constraint("ck_experiments_calibration_sha", "experiments", schema="lab")
    op.drop_column("experiments", "calibration_sha256", schema="lab")
