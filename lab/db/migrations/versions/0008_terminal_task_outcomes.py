"""Add race-safe durable terminal results alongside independently scored tasks."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_terminal_task_outcomes"
down_revision = "0007_seal_task_plan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Make task resolution a unique, append-only score-or-terminal union."""
    op.drop_constraint("ck_runs_task_plan_seal", "runs", schema="lab", type_="check")
    op.create_check_constraint(
        "ck_runs_task_plan_seal",
        "runs",
        "(task_plan_sha256 is null and task_plan_count is null) or "
        "(length(task_plan_sha256) = 64 and task_plan_count >= 0)",
        schema="lab",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION scorer.guard_task_plan_seal()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        BEGIN
            IF OLD.task_plan_sha256 IS NOT NULL AND
               (NEW.task_plan_sha256 IS DISTINCT FROM OLD.task_plan_sha256 OR
                NEW.task_plan_count IS DISTINCT FROM OLD.task_plan_count) THEN
                RAISE EXCEPTION 'sealed task plan is immutable';
            END IF;
            IF (NEW.task_plan_sha256 IS NULL) <> (NEW.task_plan_count IS NULL) OR
               (NEW.task_plan_sha256 IS NOT NULL AND
                (length(NEW.task_plan_sha256) <> 64 OR NEW.task_plan_count < 0 OR
                 (NEW.task_plan_count = 0 AND NEW.state <> 'stop_requested'))) THEN
                RAISE EXCEPTION 'empty task plans are allowed only for stop finalization';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.drop_constraint("ck_score_jobs_state", "score_jobs", schema="scorer", type_="check")
    op.create_check_constraint(
        "ck_score_jobs_state",
        "score_jobs",
        "state in ('queued','running','completed','failed','cancelled')",
        schema="scorer",
    )
    op.create_table(
        "task_completions",
        sa.Column("run_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("experiment_id", sa.String(128), primary_key=True),
        sa.Column("evaluation_kind", sa.String(24), primary_key=True),
        sa.Column("task_id", sa.String(128), primary_key=True),
        sa.Column("seed", sa.Integer, primary_key=True),
        sa.Column("completion_kind", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "experiment_id", "evaluation_kind", "task_id", "seed"],
            ["scorer.run_tasks.run_id", "scorer.run_tasks.experiment_id",
             "scorer.run_tasks.evaluation_kind", "scorer.run_tasks.task_id",
             "scorer.run_tasks.seed"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "completion_kind in ('scored','terminal')", name="ck_task_completion_kind"
        ),
        schema="scorer",
    )
    op.create_table(
        "task_terminal_outcomes",
        sa.Column("run_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("experiment_id", sa.String(128), primary_key=True),
        sa.Column("evaluation_kind", sa.String(24), primary_key=True),
        sa.Column("task_id", sa.String(128), primary_key=True),
        sa.Column("seed", sa.Integer, primary_key=True),
        sa.Column("candidate_sha256", sa.String(64), nullable=False),
        sa.Column("outcome_code", sa.String(32), nullable=False),
        sa.Column("producer_role", sa.String(32), nullable=False),
        sa.Column("score_job_id", sa.Uuid(as_uuid=True), sa.ForeignKey("scorer.score_jobs.job_id")),
        sa.Column("claim_token", sa.String(128)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "experiment_id", "evaluation_kind", "task_id", "seed"],
            ["scorer.run_tasks.run_id", "scorer.run_tasks.experiment_id",
             "scorer.run_tasks.evaluation_kind", "scorer.run_tasks.task_id",
             "scorer.run_tasks.seed"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "outcome_code in ('guard_rejected','candidate_rejected','candidate_crash',"
            "'candidate_timeout','scorer_error','cancelled')",
            name="ck_task_terminal_outcome_code",
        ),
        sa.CheckConstraint("length(candidate_sha256) = 64", name="ck_terminal_candidate_sha"),
        schema="scorer",
    )
    op.execute(
        """
        INSERT INTO scorer.task_completions
            (run_id, experiment_id, evaluation_kind, task_id, seed, completion_kind)
        SELECT run_id, experiment_id, evaluation_kind, task_id, seed, 'scored'
          FROM scorer.task_scores
        """
    )

    op.execute(
        """
        CREATE FUNCTION scorer.guard_score_job_enqueue()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE current_state text; assigned_sha text;
        BEGIN
            IF session_user <> 'swapp_lab_planner' OR
               current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'score enqueue requires Planner at READ COMMITTED';
            END IF;
            IF NEW.state <> 'queued' OR NEW.attempt <> 0 OR NEW.claimed_by IS NOT NULL OR
               NEW.lease_until IS NOT NULL THEN
                RAISE EXCEPTION 'Planner may only enqueue a fresh unclaimed score job';
            END IF;
            SELECT state INTO current_state FROM lab.runs
             WHERE run_id = NEW.run_id FOR UPDATE;
            IF NOT FOUND OR current_state <> 'running' THEN
                RAISE EXCEPTION 'score enqueue requires a running run';
            END IF;
            SELECT candidate_sha256 INTO assigned_sha FROM scorer.run_tasks
             WHERE run_id = NEW.run_id AND experiment_id = NEW.experiment_id
               AND evaluation_kind = NEW.evaluation_kind AND task_id = NEW.task_id
               AND seed = NEW.seed;
            IF NOT FOUND OR assigned_sha <> NEW.candidate_sha256 THEN
                RAISE EXCEPTION 'score job does not match trusted task assignment';
            END IF;
            IF EXISTS (SELECT 1 FROM scorer.task_completions WHERE run_id = NEW.run_id
                AND experiment_id = NEW.experiment_id AND evaluation_kind = NEW.evaluation_kind
                AND task_id = NEW.task_id AND seed = NEW.seed) THEN
                RAISE EXCEPTION 'task already has a durable resolution';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_score_jobs_only_unresolved_tasks
        BEFORE INSERT ON scorer.score_jobs
        FOR EACH ROW EXECUTE FUNCTION scorer.guard_score_job_enqueue()
        """
    )
    op.execute(
        """
        CREATE FUNCTION scorer.capture_scored_task_completion()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE run_state text;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' OR
               current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'scored task write requires the Scorer role at READ COMMITTED';
            END IF;
            SELECT state INTO run_state FROM lab.runs
             WHERE run_id = NEW.run_id FOR UPDATE;
            IF NOT FOUND OR run_state <> 'running' THEN
                RAISE EXCEPTION 'task score requires an active run';
            END IF;
            INSERT INTO scorer.task_completions
                (run_id, experiment_id, evaluation_kind, task_id, seed, completion_kind)
            VALUES
                (NEW.run_id, NEW.experiment_id, NEW.evaluation_kind, NEW.task_id,
                 NEW.seed, 'scored')
            ON CONFLICT DO NOTHING;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'task already has a durable terminal resolution';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_task_scores_completion
        BEFORE INSERT ON scorer.task_scores
        FOR EACH ROW EXECUTE FUNCTION scorer.capture_scored_task_completion()
        """
    )
    op.execute(
        """
        CREATE FUNCTION scorer.capture_terminal_task_completion()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE
            run_state text;
            assigned_sha text;
            changed integer;
            queued_job uuid;
            queued_state text;
        BEGIN
            IF current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'terminal task write requires READ COMMITTED';
            END IF;
            NEW.producer_role := session_user;
            IF session_user = 'swapp_lab_planner' THEN
                IF NEW.outcome_code NOT IN
                   ('guard_rejected','candidate_rejected','candidate_crash',
                    'candidate_timeout','cancelled') THEN
                    RAISE EXCEPTION 'Planner cannot write this terminal outcome';
                END IF;
            ELSIF session_user = 'swapp_lab_scorer' THEN
                IF NEW.outcome_code NOT IN ('candidate_rejected','scorer_error','cancelled') THEN
                    RAISE EXCEPTION 'Scorer cannot write this terminal outcome';
                END IF;
            ELSE
                RAISE EXCEPTION 'terminal task write requires Planner or Scorer role';
            END IF;
            SELECT state INTO run_state FROM lab.runs
             WHERE run_id = NEW.run_id FOR UPDATE;
            IF NOT FOUND OR
               (NEW.outcome_code = 'cancelled' AND run_state <> 'stop_requested') OR
               (NEW.outcome_code <> 'cancelled' AND run_state <> 'running') THEN
                RAISE EXCEPTION 'terminal outcome does not match the run state';
            END IF;
            SELECT candidate_sha256 INTO assigned_sha FROM scorer.run_tasks
             WHERE run_id = NEW.run_id AND experiment_id = NEW.experiment_id
               AND evaluation_kind = NEW.evaluation_kind AND task_id = NEW.task_id
               AND seed = NEW.seed;
            IF NOT FOUND OR assigned_sha <> NEW.candidate_sha256 THEN
                RAISE EXCEPTION 'terminal outcome does not match its trusted assignment';
            END IF;
            IF session_user = 'swapp_lab_planner' THEN
                SELECT job_id, state INTO queued_job, queued_state FROM scorer.score_jobs
                 WHERE run_id = NEW.run_id AND experiment_id = NEW.experiment_id
                   AND evaluation_kind = NEW.evaluation_kind AND task_id = NEW.task_id
                   AND seed = NEW.seed FOR UPDATE;
                IF NEW.outcome_code <> 'cancelled' AND FOUND THEN
                    RAISE EXCEPTION 'Planner cannot terminate a task with a score job';
                END IF;
            END IF;
            INSERT INTO scorer.task_completions
                (run_id, experiment_id, evaluation_kind, task_id, seed, completion_kind)
            VALUES
                (NEW.run_id, NEW.experiment_id, NEW.evaluation_kind, NEW.task_id,
                 NEW.seed, 'terminal')
            ON CONFLICT DO NOTHING;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'task already has a durable score or terminal resolution';
            END IF;
            IF session_user = 'swapp_lab_scorer' THEN
                IF NEW.score_job_id IS NULL OR NEW.claim_token IS NULL THEN
                    RAISE EXCEPTION 'Scorer terminal outcome requires its active job claim';
                END IF;
                UPDATE scorer.score_jobs SET
                    state = CASE WHEN NEW.outcome_code = 'cancelled'
                                 THEN 'cancelled' ELSE 'failed' END,
                    claimed_by = NULL, lease_until = NULL, error_code = NEW.outcome_code,
                    updated_at = clock_timestamp()
                 WHERE job_id = NEW.score_job_id AND run_id = NEW.run_id
                   AND experiment_id = NEW.experiment_id AND evaluation_kind = NEW.evaluation_kind
                   AND task_id = NEW.task_id AND seed = NEW.seed
                   AND candidate_sha256 = NEW.candidate_sha256 AND state = 'running'
                   AND claimed_by = NEW.claim_token AND lease_until > clock_timestamp();
                GET DIAGNOSTICS changed = ROW_COUNT;
                IF changed <> 1 THEN
                    RAISE EXCEPTION 'Scorer job claim or lease is no longer current';
                END IF;
            ELSIF NEW.score_job_id IS NOT NULL THEN
                IF NEW.outcome_code <> 'cancelled' OR NEW.claim_token IS NOT NULL THEN
                    RAISE EXCEPTION 'Planner may attach only queued cancellation jobs';
                END IF;
                IF queued_job IS DISTINCT FROM NEW.score_job_id OR queued_state <> 'queued' THEN
                    RAISE EXCEPTION 'Planner may cancel only the task current queued job';
                END IF;
                UPDATE scorer.score_jobs SET state = 'cancelled', claimed_by = NULL,
                    lease_until = NULL, error_code = 'cancelled', updated_at = clock_timestamp()
                 WHERE job_id = NEW.score_job_id AND run_id = NEW.run_id
                   AND experiment_id = NEW.experiment_id AND evaluation_kind = NEW.evaluation_kind
                   AND task_id = NEW.task_id AND seed = NEW.seed
                   AND candidate_sha256 = NEW.candidate_sha256 AND state = 'queued';
                GET DIAGNOSTICS changed = ROW_COUNT;
                IF changed <> 1 THEN
                    RAISE EXCEPTION 'only a queued score job can be cancelled by Planner';
                END IF;
            ELSIF session_user = 'swapp_lab_planner' AND queued_job IS NOT NULL THEN
                RAISE EXCEPTION 'Planner must cancel the task current queued job';
            ELSIF NEW.claim_token IS NOT NULL THEN
                RAISE EXCEPTION 'job claim token supplied without a score job';
            END IF;
            NEW.claim_token := NULL;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_terminal_task_completion
        BEFORE INSERT ON scorer.task_terminal_outcomes
        FOR EACH ROW EXECUTE FUNCTION scorer.capture_terminal_task_completion()
        """
    )
    op.execute("REVOKE ALL ON scorer.task_completions FROM PUBLIC")
    op.execute("REVOKE UPDATE, DELETE ON scorer.task_scores FROM swapp_lab_scorer")
    op.execute("REVOKE ALL ON FUNCTION scorer.guard_score_job_enqueue() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION scorer.capture_scored_task_completion() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION scorer.capture_terminal_task_completion() FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA scorer TO swapp_lab_planner")
    op.execute("GRANT INSERT, SELECT ON scorer.task_terminal_outcomes TO swapp_lab_planner")
    op.execute("GRANT USAGE ON SCHEMA scorer TO swapp_lab_scorer")
    op.execute("GRANT INSERT, SELECT ON scorer.task_terminal_outcomes TO swapp_lab_scorer")


def downgrade() -> None:
    """Drop terminal outcomes and restore nonempty-only plan sealing."""
    op.execute("DROP TRIGGER trg_score_jobs_only_unresolved_tasks ON scorer.score_jobs")
    op.execute("DROP TRIGGER trg_terminal_task_completion ON scorer.task_terminal_outcomes")
    op.execute("DROP TRIGGER trg_task_scores_completion ON scorer.task_scores")
    op.execute("DROP FUNCTION scorer.guard_score_job_enqueue()")
    op.execute("DROP FUNCTION scorer.capture_terminal_task_completion()")
    op.execute("DROP FUNCTION scorer.capture_scored_task_completion()")
    op.execute("REVOKE INSERT, SELECT ON scorer.task_terminal_outcomes FROM swapp_lab_scorer")
    op.execute("REVOKE INSERT, SELECT ON scorer.task_terminal_outcomes FROM swapp_lab_planner")
    op.execute("GRANT UPDATE, DELETE ON scorer.task_scores TO swapp_lab_scorer")
    op.drop_table("task_terminal_outcomes", schema="scorer")
    op.drop_table("task_completions", schema="scorer")
    op.drop_constraint("ck_score_jobs_state", "score_jobs", schema="scorer", type_="check")
    op.create_check_constraint(
        "ck_score_jobs_state", "score_jobs", "state in ('queued','running','completed','failed')",
        schema="scorer",
    )
    op.drop_constraint("ck_runs_task_plan_seal", "runs", schema="lab", type_="check")
    op.create_check_constraint(
        "ck_runs_task_plan_seal", "runs",
        "(task_plan_sha256 is null and task_plan_count is null) or "
        "(length(task_plan_sha256) = 64 and task_plan_count > 0)",
        schema="lab",
    )
