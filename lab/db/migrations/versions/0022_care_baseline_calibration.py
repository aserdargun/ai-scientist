"""Persist private, measured Farm B baseline calibration and registry binding."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0022_care_baseline_calibration"
down_revision = "0021_run_end_unavailable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "care_baseline_calibration_jobs",
        sa.Column("calibration_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("suite_id", sa.String(128), nullable=False),
        sa.Column("suite_version", sa.Integer, nullable=False),
        sa.Column("suite_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("calibration_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("development_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("epsilon", sa.Float, nullable=False),
        sa.Column("source_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("source_archive_sha256", sa.String(64), nullable=False),
        sa.Column("task_weight_policy", sa.String(64), nullable=False),
        sa.Column("task_weight_policy_sha256", sa.String(64), nullable=False),
        sa.Column(
            "algorithm_sources_json", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("harness_sha256", sa.String(64), nullable=False),
        sa.Column("image_sha256", sa.String(64), nullable=False),
        sa.Column("wall_limit_seconds", sa.Integer, nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("suite_id", "suite_version", name="uq_care_calibration_suite_version"),
        sa.CheckConstraint(
            "suite_id = 'care-farm-b-measured'", name="ck_care_calibration_measured_suite"
        ),
        sa.CheckConstraint("suite_version > 0", name="ck_care_calibration_suite_version"),
        sa.CheckConstraint(
            "wall_limit_seconds between 1 and 14400", name="ck_care_calibration_wall"
        ),
        sa.CheckConstraint("epsilon >= 0 and epsilon < 1", name="ck_care_calibration_epsilon"),
        sa.CheckConstraint(
            "task_weight_policy = 'farm-b-equal-task-weight-1-over-15.v1'",
            name="ck_care_calibration_weight_policy",
        ),
        sa.CheckConstraint(
            "task_weight_policy_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_policy_sha"
        ),
        sa.CheckConstraint(
            "suite_manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_suite_sha"
        ),
        sa.CheckConstraint(
            "calibration_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_care_calibration_manifest_sha",
        ),
        sa.CheckConstraint(
            "development_manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_dev_sha"
        ),
        sa.CheckConstraint(
            "source_manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_source_sha"
        ),
        sa.CheckConstraint(
            "source_archive_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_archive_sha"
        ),
        sa.CheckConstraint(
            "harness_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_harness_sha"
        ),
        sa.CheckConstraint("image_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_image_sha"),
        schema="scorer",
    )
    op.create_table(
        "care_baseline_calibration_tasks",
        sa.Column("calibration_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("task_key", sa.String(32), primary_key=True),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("dataset_id", sa.String(128), nullable=False),
        sa.Column("split_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(256), nullable=False),
        sa.Column("profile_sha256", sa.String(64), nullable=False),
        sa.Column("family", sa.String(8), nullable=False),
        sa.Column("task_weight", sa.Float, nullable=False),
        sa.Column("train_sha256", sa.String(64), nullable=False),
        sa.Column("evaluation_sha256", sa.String(64), nullable=False),
        sa.Column("evaluation_rows", sa.Integer, nullable=False),
        sa.Column("labels_sha256", sa.String(64), nullable=False),
        sa.Column("semantics_sha256", sa.String(64), nullable=False),
        sa.Column("source_member_sha256", sa.String(64), nullable=False),
        sa.Column("sliding_window", sa.Integer, nullable=False),
        sa.Column("sampling_s", sa.Integer, nullable=False),
        sa.Column("context_json", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.ForeignKeyConstraint(
            ["calibration_id"], ["scorer.care_baseline_calibration_jobs.calibration_id"]
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id", "split_id", "session_id"],
            [
                "scorer.dataset_profiles.dataset_id",
                "scorer.dataset_profiles.split_id",
                "scorer.dataset_profiles.session_id",
            ],
        ),
        sa.UniqueConstraint("calibration_id", "dataset_id", "split_id", "session_id"),
        sa.CheckConstraint("task_key ~ '^[0-9a-f]{32}$'", name="ck_care_calibration_task_key"),
        sa.CheckConstraint(
            "profile_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_profile_sha"
        ),
        sa.CheckConstraint("train_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_train_sha"),
        sa.CheckConstraint(
            "evaluation_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_eval_sha"
        ),
        sa.CheckConstraint(
            "labels_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_labels_sha"
        ),
        sa.CheckConstraint(
            "semantics_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_semantics_sha"
        ),
        sa.CheckConstraint(
            "source_member_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_member_sha"
        ),
        sa.CheckConstraint("family in ('PDM','NRM')", name="ck_care_calibration_family"),
        sa.CheckConstraint(
            "abs(task_weight - 0.06666666666666667) < 1e-12", name="ck_care_calibration_weight"
        ),
        sa.CheckConstraint(
            "sliding_window > 0 and sampling_s > 0 and evaluation_rows > 0",
            name="ck_care_calibration_task_shape",
        ),
        schema="scorer",
    )
    op.create_table(
        "care_baseline_calibration_cells",
        sa.Column("calibration_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("task_key", sa.String(32), primary_key=True),
        sa.Column("algorithm", sa.String(32), primary_key=True),
        sa.Column("seed", sa.Integer, primary_key=True),
        sa.Column("baseline_source_sha256", sa.String(64), nullable=False),
        sa.Column("harness_sha256", sa.String(64), nullable=False),
        sa.Column("image_sha256", sa.String(64), nullable=False),
        sa.Column("profile_sha256", sa.String(64), nullable=False),
        sa.Column("train_sha256", sa.String(64), nullable=False),
        sa.Column("evaluation_sha256", sa.String(64), nullable=False),
        sa.Column("labels_sha256", sa.String(64), nullable=False),
        sa.Column("semantics_sha256", sa.String(64), nullable=False),
        sa.Column("fit_context_sha256", sa.String(64), nullable=False),
        sa.Column("fit_artifact_sha256", sa.String(64), nullable=False),
        sa.Column("score_document_sha256", sa.String(64), nullable=False),
        sa.Column("policy_json", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("raw_task_score", sa.Float, nullable=False),
        sa.Column(
            "auxiliary_metrics_json", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("fit_seconds", sa.Float, nullable=False),
        sa.Column("score_seconds", sa.Float, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["calibration_id", "task_key"],
            [
                "scorer.care_baseline_calibration_tasks.calibration_id",
                "scorer.care_baseline_calibration_tasks.task_key",
            ],
        ),
        sa.CheckConstraint(
            "algorithm in ('robust_z','iforest','ecod_train_frozen')",
            name="ck_care_calibration_algorithm",
        ),
        sa.CheckConstraint("seed in (0,1,2)", name="ck_care_calibration_seed"),
        sa.CheckConstraint(
            "raw_task_score >= 0 and raw_task_score <= 1", name="ck_care_calibration_score"
        ),
        sa.CheckConstraint(
            "fit_seconds >= 0 and score_seconds >= 0", name="ck_care_calibration_elapsed"
        ),
        schema="scorer",
    )
    op.create_table(
        "care_baseline_calibration_task_summaries",
        sa.Column("calibration_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("task_key", sa.String(32), primary_key=True),
        sa.Column("base_score", sa.Float, nullable=False),
        sa.Column("reference_score", sa.Float, nullable=False),
        sa.Column("task_weight", sa.Float, nullable=False),
        sa.ForeignKeyConstraint(
            ["calibration_id", "task_key"],
            [
                "scorer.care_baseline_calibration_tasks.calibration_id",
                "scorer.care_baseline_calibration_tasks.task_key",
            ],
        ),
        sa.CheckConstraint(
            "base_score >= 0 and base_score <= reference_score and reference_score <= 1",
            name="ck_care_calibration_summary_scores",
        ),
        sa.CheckConstraint(
            "abs(task_weight - 0.06666666666666667) < 1e-12",
            name="ck_care_calibration_summary_weight",
        ),
        schema="scorer",
    )
    op.create_table(
        "care_baseline_calibration_freezes",
        sa.Column("calibration_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("calibration_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("task_count", sa.Integer, nullable=False),
        sa.Column("cell_count", sa.Integer, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["calibration_id"], ["scorer.care_baseline_calibration_jobs.calibration_id"]
        ),
        sa.CheckConstraint(
            "manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_freeze_manifest_sha"
        ),
        sa.CheckConstraint(
            "calibration_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_freeze_sha"
        ),
        sa.CheckConstraint("task_count = 15", name="ck_care_calibration_freeze_tasks"),
        sa.CheckConstraint("cell_count = 135", name="ck_care_calibration_freeze_cells"),
        schema="scorer",
    )

    op.add_column(
        "holdout_suite_versions",
        sa.Column("care_calibration_id", sa.Uuid(as_uuid=True)),
        schema="scorer",
    )
    op.create_foreign_key(
        "fk_holdout_suite_care_calibration",
        "holdout_suite_versions",
        "care_baseline_calibration_freezes",
        ["care_calibration_id"],
        ["calibration_id"],
        source_schema="scorer",
        referent_schema="scorer",
        ondelete="RESTRICT",
    )

    op.execute(
        "REVOKE ALL ON scorer.care_baseline_calibration_jobs, "
        "scorer.care_baseline_calibration_tasks, scorer.care_baseline_calibration_cells, "
        "scorer.care_baseline_calibration_task_summaries, "
        "scorer.care_baseline_calibration_freezes FROM PUBLIC, "
        "swapp_lab_director, swapp_lab_planner"
    )
    op.execute(
        "GRANT SELECT, INSERT ON scorer.care_baseline_calibration_jobs, "
        "scorer.care_baseline_calibration_tasks TO swapp_lab_migrator"
    )
    op.execute(
        "GRANT SELECT, INSERT ON scorer.care_baseline_calibration_cells, "
        "scorer.care_baseline_calibration_task_summaries, "
        "scorer.care_baseline_calibration_freezes TO swapp_lab_scorer"
    )
    op.execute(
        "GRANT SELECT ON scorer.care_baseline_calibration_jobs, "
        "scorer.care_baseline_calibration_tasks, scorer.care_baseline_calibration_cells, "
        "scorer.care_baseline_calibration_task_summaries, "
        "scorer.care_baseline_calibration_freezes TO swapp_lab_scorer"
    )
    op.execute(
        """
        CREATE FUNCTION lab.lock_care_calibration_job(p_calibration_id uuid)
        RETURNS timestamptz LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE stored_deadline timestamptz;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'CARE calibration lock requires Scorer';
            END IF;
            SELECT deadline_at INTO stored_deadline
              FROM scorer.care_baseline_calibration_jobs
             WHERE calibration_id=p_calibration_id FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'CARE calibration job is missing';
            END IF;
            RETURN stored_deadline;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION lab.lock_care_calibration_job(uuid) FROM PUBLIC, "
        "swapp_lab_director, swapp_lab_planner"
    )
    op.execute("GRANT EXECUTE ON FUNCTION lab.lock_care_calibration_job(uuid) TO swapp_lab_scorer")
    op.execute(
        "GRANT SELECT, INSERT (care_calibration_id) ON scorer.holdout_suite_versions "
        "TO swapp_lab_scorer"
    )

    op.execute(
        """
        CREATE FUNCTION lab.guard_care_calibration_immutable()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'CARE calibration records are immutable';
            END IF;
            IF TG_TABLE_NAME IN (
                'care_baseline_calibration_jobs','care_baseline_calibration_tasks'
            ) THEN
                IF session_user <> 'swapp_lab_migrator' THEN
                    RAISE EXCEPTION 'CARE calibration provisioning requires Migrator';
                END IF;
            ELSIF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'CARE calibration receipts require Scorer';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table_name in (
        "care_baseline_calibration_jobs",
        "care_baseline_calibration_tasks",
        "care_baseline_calibration_cells",
        "care_baseline_calibration_task_summaries",
        "care_baseline_calibration_freezes",
    ):
        op.execute(
            f"CREATE TRIGGER {table_name}_immutable BEFORE INSERT OR UPDATE OR DELETE "
            f"ON scorer.{table_name} FOR EACH ROW "
            "EXECUTE FUNCTION lab.guard_care_calibration_immutable()"
        )

    op.execute(
        """
        CREATE FUNCTION lab.guard_care_calibration_task_input()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE job scorer.care_baseline_calibration_jobs%ROWTYPE;
                existing scorer.care_baseline_calibration_tasks%ROWTYPE;
                profile scorer.dataset_profiles%ROWTYPE;
                semantics scorer.dataset_task_semantics%ROWTYPE; label_count integer;
        BEGIN
            SELECT * INTO job FROM scorer.care_baseline_calibration_jobs
             WHERE calibration_id=NEW.calibration_id FOR UPDATE;
            SELECT * INTO existing FROM scorer.care_baseline_calibration_tasks
             WHERE calibration_id=NEW.calibration_id AND task_key=NEW.task_key;
            IF job.calibration_id IS NULL THEN
                RAISE EXCEPTION 'CARE calibration job is missing';
            END IF;
            IF EXISTS (SELECT 1 FROM scorer.care_baseline_calibration_freezes
                       WHERE calibration_id=NEW.calibration_id)
               OR clock_timestamp() >= job.deadline_at THEN
                IF existing.task_key IS NULL
                   OR to_jsonb(existing) IS DISTINCT FROM to_jsonb(NEW) THEN
                    RAISE EXCEPTION 'CARE task inputs are sealed or job deadline expired';
                END IF;
                RETURN NEW;
            END IF;
            SELECT * INTO profile FROM scorer.dataset_profiles WHERE dataset_id=NEW.dataset_id
             AND split_id=NEW.split_id AND session_id=NEW.session_id;
            SELECT * INTO semantics FROM scorer.dataset_task_semantics
             WHERE dataset_id=NEW.dataset_id
             AND split_id=NEW.split_id AND session_id=NEW.session_id;
            SELECT count(*) INTO label_count FROM scorer.dataset_labels
             WHERE dataset_id=NEW.dataset_id
             AND split_id=NEW.split_id AND session_id=NEW.session_id;
            IF job.calibration_id IS NULL OR profile.visibility <> 'holdout'
               OR profile.profile_sha256 IS DISTINCT FROM NEW.profile_sha256
               OR profile.task_family IS DISTINCT FROM NEW.family
               OR profile.sample_count IS DISTINCT FROM NEW.evaluation_rows
               OR semantics.semantics_sha256 IS DISTINCT FROM NEW.semantics_sha256
               OR semantics.task_family IS DISTINCT FROM NEW.family
               OR semantics.sampling_s IS DISTINCT FROM NEW.sampling_s
               OR label_count IS DISTINCT FROM NEW.evaluation_rows THEN
                RAISE EXCEPTION 'CARE calibration input differs from installed private profile';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER care_calibration_task_input_guard BEFORE INSERT "
        "ON scorer.care_baseline_calibration_tasks FOR EACH ROW "
        "EXECUTE FUNCTION lab.guard_care_calibration_task_input()"
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_care_calibration_cell()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE job scorer.care_baseline_calibration_jobs%ROWTYPE;
                task scorer.care_baseline_calibration_tasks%ROWTYPE;
                existing scorer.care_baseline_calibration_cells%ROWTYPE;
        BEGIN
            SELECT * INTO job FROM scorer.care_baseline_calibration_jobs
             WHERE calibration_id=NEW.calibration_id FOR UPDATE;
            SELECT * INTO task FROM scorer.care_baseline_calibration_tasks
             WHERE calibration_id=NEW.calibration_id AND task_key=NEW.task_key;
            SELECT * INTO existing FROM scorer.care_baseline_calibration_cells
             WHERE calibration_id=NEW.calibration_id AND task_key=NEW.task_key
               AND algorithm=NEW.algorithm AND seed=NEW.seed;
            IF EXISTS (SELECT 1 FROM scorer.care_baseline_calibration_freezes
                       WHERE calibration_id=NEW.calibration_id) THEN
                IF existing.task_key IS NULL
                   OR (to_jsonb(existing) - 'created_at') IS DISTINCT FROM
                      (to_jsonb(NEW) - 'created_at') THEN
                    RAISE EXCEPTION 'CARE calibration cells are sealed by the freeze';
                END IF;
                RETURN NEW;
            END IF;
            IF job.calibration_id IS NULL OR task.task_key IS NULL
               OR clock_timestamp() >= job.deadline_at
               OR job.harness_sha256 IS DISTINCT FROM NEW.harness_sha256
               OR job.image_sha256 IS DISTINCT FROM NEW.image_sha256
               OR task.profile_sha256 IS DISTINCT FROM NEW.profile_sha256
               OR task.train_sha256 IS DISTINCT FROM NEW.train_sha256
               OR task.evaluation_sha256 IS DISTINCT FROM NEW.evaluation_sha256
               OR task.labels_sha256 IS DISTINCT FROM NEW.labels_sha256
               OR task.semantics_sha256 IS DISTINCT FROM NEW.semantics_sha256
               OR NEW.baseline_source_sha256 IS DISTINCT FROM
                    job.algorithm_sources_json->NEW.algorithm->>'source_sha256' THEN
                RAISE EXCEPTION 'CARE calibration cell identity or deadline is invalid';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER care_calibration_cell_guard BEFORE INSERT "
        "ON scorer.care_baseline_calibration_cells FOR EACH ROW "
        "EXECUTE FUNCTION lab.guard_care_calibration_cell()"
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_care_calibration_summary()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE task scorer.care_baseline_calibration_tasks%ROWTYPE;
                job scorer.care_baseline_calibration_jobs%ROWTYPE;
                existing scorer.care_baseline_calibration_task_summaries%ROWTYPE;
                robust_mean double precision; forest_mean double precision;
                ecod_mean double precision; cell_count integer;
        BEGIN
            SELECT * INTO job FROM scorer.care_baseline_calibration_jobs
             WHERE calibration_id=NEW.calibration_id FOR UPDATE;
            SELECT * INTO existing FROM scorer.care_baseline_calibration_task_summaries
             WHERE calibration_id=NEW.calibration_id AND task_key=NEW.task_key;
            IF EXISTS (SELECT 1 FROM scorer.care_baseline_calibration_freezes
                       WHERE calibration_id=NEW.calibration_id) THEN
                IF existing.task_key IS NULL
                   OR (to_jsonb(existing) - 'created_at') IS DISTINCT FROM
                      (to_jsonb(NEW) - 'created_at') THEN
                    RAISE EXCEPTION 'CARE calibration summaries are sealed by the freeze';
                END IF;
                RETURN NEW;
            END IF;
            SELECT * INTO task FROM scorer.care_baseline_calibration_tasks
             WHERE calibration_id=NEW.calibration_id AND task_key=NEW.task_key;
            SELECT count(*),avg(raw_task_score) FILTER (WHERE algorithm='robust_z'),
                   avg(raw_task_score) FILTER (WHERE algorithm='iforest'),
                   avg(raw_task_score) FILTER (WHERE algorithm='ecod_train_frozen')
              INTO cell_count,robust_mean,forest_mean,ecod_mean
              FROM scorer.care_baseline_calibration_cells
             WHERE calibration_id=NEW.calibration_id AND task_key=NEW.task_key;
            IF cell_count <> 9 OR robust_mean IS NULL OR forest_mean IS NULL OR ecod_mean IS NULL
               OR abs(NEW.base_score-robust_mean) > 1e-12
               OR abs(NEW.reference_score-greatest(robust_mean,forest_mean,ecod_mean)) > 1e-12
               OR NEW.task_weight IS DISTINCT FROM task.task_weight THEN
                RAISE EXCEPTION 'CARE calibration summary is not derived from nine measured cells';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER care_calibration_summary_guard BEFORE INSERT "
        "ON scorer.care_baseline_calibration_task_summaries FOR EACH ROW "
        "EXECUTE FUNCTION lab.guard_care_calibration_summary()"
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_care_calibration_freeze()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE job scorer.care_baseline_calibration_jobs%ROWTYPE;
                task_count integer; cell_count integer; summary_count integer;
                algorithm_name text; seed_value integer; algorithm_count integer;
        BEGIN
            SELECT * INTO job FROM scorer.care_baseline_calibration_jobs
             WHERE calibration_id=NEW.calibration_id FOR UPDATE;
            SELECT count(*) INTO task_count FROM scorer.care_baseline_calibration_tasks
             WHERE calibration_id=NEW.calibration_id;
            SELECT count(*) INTO cell_count FROM scorer.care_baseline_calibration_cells
             WHERE calibration_id=NEW.calibration_id;
            SELECT count(*) INTO summary_count FROM scorer.care_baseline_calibration_task_summaries
             WHERE calibration_id=NEW.calibration_id;
            IF job.calibration_id IS NULL
               OR task_count <> 15 OR cell_count <> 135 OR summary_count <> 15
               OR NEW.manifest_sha256 IS DISTINCT FROM job.calibration_manifest_sha256
               OR NEW.task_count <> task_count OR NEW.cell_count <> cell_count THEN
                RAISE EXCEPTION 'CARE calibration is incomplete or differs from its immutable job';
            END IF;
            FOREACH algorithm_name IN ARRAY ARRAY['robust_z','iforest','ecod_train_frozen'] LOOP
                FOREACH seed_value IN ARRAY ARRAY[0,1,2] LOOP
                    SELECT count(*) INTO algorithm_count FROM scorer.care_baseline_calibration_cells
                     WHERE calibration_id=NEW.calibration_id AND algorithm=algorithm_name
                       AND seed=seed_value;
                    IF algorithm_count <> 15 THEN
                        RAISE EXCEPTION 'CARE calibration lacks exact algorithm/seed coverage';
                    END IF;
                END LOOP;
            END LOOP;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER care_calibration_freeze_guard BEFORE INSERT "
        "ON scorer.care_baseline_calibration_freezes FOR EACH ROW "
        "EXECUTE FUNCTION lab.guard_care_calibration_freeze()"
    )

    op.execute(
        """
        CREATE FUNCTION lab.guard_care_measured_suite_version()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE job scorer.care_baseline_calibration_jobs%ROWTYPE;
                frozen scorer.care_baseline_calibration_freezes%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'holdout suite registration requires Scorer';
            END IF;
            IF NEW.suite_id <> 'care-farm-b-measured' THEN
                IF NEW.care_calibration_id IS NOT NULL THEN
                    RAISE EXCEPTION 'CARE calibration may bind only the measured Farm B suite';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.care_calibration_id IS NULL THEN
                RAISE EXCEPTION 'measured Farm B suite requires a calibration receipt';
            END IF;
            SELECT * INTO job FROM scorer.care_baseline_calibration_jobs
             WHERE calibration_id=NEW.care_calibration_id;
            SELECT * INTO frozen FROM scorer.care_baseline_calibration_freezes
             WHERE calibration_id=NEW.care_calibration_id;
            IF job.calibration_id IS NULL OR frozen.calibration_id IS NULL
               OR job.suite_id IS DISTINCT FROM NEW.suite_id
               OR job.suite_version IS DISTINCT FROM NEW.suite_version
               OR job.suite_manifest_sha256 IS DISTINCT FROM NEW.manifest_sha256
               OR job.development_manifest_sha256 IS DISTINCT FROM NEW.development_manifest_sha256
               OR job.epsilon IS DISTINCT FROM NEW.epsilon
               OR NEW.task_count <> 15 THEN
                RAISE EXCEPTION 'measured Farm B suite identity differs from its calibration';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER care_measured_suite_version_guard BEFORE INSERT "
        "ON scorer.holdout_suite_versions FOR EACH ROW "
        "EXECUTE FUNCTION lab.guard_care_measured_suite_version()"
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_care_measured_suite_task()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE version_row scorer.holdout_suite_versions%ROWTYPE;
                task scorer.care_baseline_calibration_tasks%ROWTYPE;
                summary scorer.care_baseline_calibration_task_summaries%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'holdout suite task registration requires Scorer';
            END IF;
            SELECT * INTO version_row FROM scorer.holdout_suite_versions
             WHERE suite_id=NEW.suite_id AND suite_version=NEW.suite_version;
            IF NEW.suite_id <> 'care-farm-b-measured' THEN RETURN NEW; END IF;
            SELECT * INTO task FROM scorer.care_baseline_calibration_tasks
             WHERE calibration_id=version_row.care_calibration_id AND task_key=NEW.task_key;
            SELECT * INTO summary FROM scorer.care_baseline_calibration_task_summaries
             WHERE calibration_id=version_row.care_calibration_id AND task_key=NEW.task_key;
            IF version_row.care_calibration_id IS NULL OR task.task_key IS NULL
               OR summary.task_key IS NULL
               OR ROW(NEW.dataset_id,NEW.split_id,NEW.session_id,NEW.profile_sha256,NEW.family,
                      NEW.task_weight,NEW.base_score,NEW.reference_score,NEW.sliding_window,
                      NEW.sampling_s,NEW.train_sha256,NEW.evaluation_sha256,NEW.semantics_sha256,
                      NEW.context_json)
                  IS DISTINCT FROM
                  ROW(task.dataset_id,task.split_id,task.session_id,task.profile_sha256,task.family,
                      task.task_weight,summary.base_score,summary.reference_score,task.sliding_window,
                      task.sampling_s,task.train_sha256,task.evaluation_sha256,task.semantics_sha256,
                      task.context_json) THEN
                RAISE EXCEPTION 'measured Farm B task differs from its calibrated private input';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER care_measured_suite_task_guard BEFORE INSERT "
        "ON scorer.holdout_suite_tasks FOR EACH ROW "
        "EXECUTE FUNCTION lab.guard_care_measured_suite_task()"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION lab.lock_care_calibration_job(uuid)")
    op.execute("DROP TRIGGER care_measured_suite_task_guard ON scorer.holdout_suite_tasks")
    op.execute("DROP FUNCTION lab.guard_care_measured_suite_task()")
    op.execute("DROP TRIGGER care_measured_suite_version_guard ON scorer.holdout_suite_versions")
    op.execute("DROP FUNCTION lab.guard_care_measured_suite_version()")
    op.execute(
        "DROP TRIGGER care_calibration_freeze_guard ON scorer.care_baseline_calibration_freezes"
    )
    op.execute("DROP FUNCTION lab.guard_care_calibration_freeze()")
    op.execute(
        "DROP TRIGGER care_calibration_summary_guard "
        "ON scorer.care_baseline_calibration_task_summaries"
    )
    op.execute("DROP FUNCTION lab.guard_care_calibration_summary()")
    op.execute("DROP TRIGGER care_calibration_cell_guard ON scorer.care_baseline_calibration_cells")
    op.execute("DROP FUNCTION lab.guard_care_calibration_cell()")
    op.execute(
        "DROP TRIGGER care_calibration_task_input_guard ON scorer.care_baseline_calibration_tasks"
    )
    op.execute("DROP FUNCTION lab.guard_care_calibration_task_input()")
    for table_name in (
        "care_baseline_calibration_jobs",
        "care_baseline_calibration_tasks",
        "care_baseline_calibration_cells",
        "care_baseline_calibration_task_summaries",
        "care_baseline_calibration_freezes",
    ):
        op.execute(f"DROP TRIGGER {table_name}_immutable ON scorer.{table_name}")
    op.execute("DROP FUNCTION lab.guard_care_calibration_immutable()")
    op.drop_constraint(
        "fk_holdout_suite_care_calibration",
        "holdout_suite_versions",
        schema="scorer",
        type_="foreignkey",
    )
    op.drop_column("holdout_suite_versions", "care_calibration_id", schema="scorer")
    op.drop_table("care_baseline_calibration_freezes", schema="scorer")
    op.drop_table("care_baseline_calibration_task_summaries", schema="scorer")
    op.drop_table("care_baseline_calibration_cells", schema="scorer")
    op.drop_table("care_baseline_calibration_tasks", schema="scorer")
    op.drop_table("care_baseline_calibration_jobs", schema="scorer")
