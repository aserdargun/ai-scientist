"""Bind verified public development suite to unchanged measured private Farm B freeze."""

from alembic import op

revision = "0032_care_development_binding"
down_revision = "0031_stopped_proposal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION lab.guard_care_measured_suite_version()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE job scorer.care_baseline_calibration_jobs%ROWTYPE;
                frozen scorer.care_baseline_calibration_freezes%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'holdout suite registration requires Scorer';
            END IF;
            IF NEW.suite_id NOT IN ('care-farm-b-measured','public-ad-v1') THEN
                IF NEW.care_calibration_id IS NOT NULL THEN
                    RAISE EXCEPTION 'CARE calibration may bind only the measured Farm B suite';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.care_calibration_id IS NULL THEN
                IF NEW.suite_id='public-ad-v1' THEN RETURN NEW; END IF;
                RAISE EXCEPTION 'measured Farm B suite requires a calibration receipt';
            END IF;
            SELECT * INTO job FROM scorer.care_baseline_calibration_jobs
             WHERE calibration_id=NEW.care_calibration_id;
            SELECT * INTO frozen FROM scorer.care_baseline_calibration_freezes
             WHERE calibration_id=NEW.care_calibration_id;
            IF job.calibration_id IS NULL OR frozen.calibration_id IS NULL
               OR NOT ((NEW.suite_id='care-farm-b-measured'
                        AND job.suite_id=NEW.suite_id AND job.suite_version=NEW.suite_version)
                       OR (NEW.suite_id='public-ad-v1' AND NEW.suite_version=3
                           AND job.suite_id='care-farm-b-measured'))
               OR job.suite_manifest_sha256 IS DISTINCT FROM NEW.manifest_sha256
               OR job.development_manifest_sha256 IS DISTINCT FROM NEW.development_manifest_sha256
               OR job.epsilon IS DISTINCT FROM NEW.epsilon
               OR NEW.task_count <> 15 OR frozen.cell_count<>135 OR frozen.task_count<>15
               OR frozen.manifest_sha256 IS DISTINCT FROM job.calibration_manifest_sha256
               OR (SELECT count(*) FROM scorer.care_baseline_calibration_cells
                    WHERE calibration_id=job.calibration_id)<>135 THEN
                RAISE EXCEPTION 'measured Farm B suite identity differs from its calibration';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION lab.guard_care_measured_suite_task()
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
            IF version_row.care_calibration_id IS NULL THEN RETURN NEW; END IF;
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
    """)


def downgrade() -> None:
    raise RuntimeError("removing measured development bindings is unsupported")
