"""Allow cancellation of a drained stop-requested Scorer generation."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_terminal_recovery_fence"
down_revision = "0009_scorer_invocation_fence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Record the fresh recovery writer separately from its drained claim."""
    op.add_column(
        "task_terminal_outcomes",
        sa.Column("recovery_invocation_id", sa.String(32)),
        schema="scorer",
    )
    op.create_check_constraint(
        "ck_task_terminal_recovery_invocation",
        "task_terminal_outcomes",
        "recovery_invocation_id is null or length(recovery_invocation_id) = 32",
        schema="scorer",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION scorer.guard_terminal_task_invocation()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE run_state text; claim scorer.score_jobs%ROWTYPE;
        BEGIN
            IF session_user = 'swapp_lab_planner' THEN
                IF NEW.recovery_invocation_id IS NOT NULL THEN
                    RAISE EXCEPTION 'Planner cannot provide a recovery invocation';
                END IF;
                RETURN NEW;
            END IF;
            IF session_user <> 'swapp_lab_scorer' OR NEW.score_job_id IS NULL OR
               NEW.claim_token IS NULL OR NEW.worker_invocation_id IS NULL OR
               NEW.worker_invocation_id !~ '^[0-9a-f]{32}$' THEN
                RAISE EXCEPTION 'Scorer terminal outcome requires a bound job invocation';
            END IF;
            IF NEW.recovery_invocation_id IS NOT NULL AND
               (NEW.outcome_code <> 'cancelled' OR
                NEW.recovery_invocation_id !~ '^[0-9a-f]{32}$' OR
                NEW.recovery_invocation_id = NEW.worker_invocation_id) THEN
                RAISE EXCEPTION 'recovery requires a distinct current cancellation invocation';
            END IF;
            SELECT state INTO run_state FROM lab.runs
             WHERE run_id = NEW.run_id FOR UPDATE;
            IF NOT FOUND OR
               (NEW.outcome_code = 'cancelled' AND run_state IS DISTINCT FROM 'stop_requested') OR
               (NEW.outcome_code <> 'cancelled' AND run_state IS DISTINCT FROM 'running') THEN
                RAISE EXCEPTION 'terminal outcome does not match the run state';
            END IF;
            SELECT * INTO claim FROM scorer.score_jobs
             WHERE job_id = NEW.score_job_id FOR UPDATE;
            IF NOT FOUND OR claim.state IS DISTINCT FROM 'running' OR
               claim.claimed_by IS DISTINCT FROM NEW.claim_token OR
               claim.lease_until IS NULL OR
               (NEW.recovery_invocation_id IS NULL AND
                    claim.lease_until <= clock_timestamp()) OR
               claim.claim_invocation_id IS DISTINCT FROM NEW.worker_invocation_id OR
               claim.claim_unit IS DISTINCT FROM 'swapp-ai-scientist-scorer-' ||
                    replace(claim.job_id::text, '-', '') || '.service' OR
               claim.run_id IS DISTINCT FROM NEW.run_id OR
               claim.experiment_id IS DISTINCT FROM NEW.experiment_id OR
               claim.evaluation_kind IS DISTINCT FROM NEW.evaluation_kind OR
               claim.task_id IS DISTINCT FROM NEW.task_id OR
               claim.seed IS DISTINCT FROM NEW.seed OR
               claim.candidate_sha256 IS DISTINCT FROM NEW.candidate_sha256 THEN
                RAISE EXCEPTION 'terminal result has no current matching Scorer invocation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION scorer.capture_terminal_task_completion_v9()
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
                UPDATE scorer.score_jobs SET state = CASE WHEN NEW.outcome_code = 'cancelled'
                        THEN 'cancelled' ELSE 'failed' END,
                    claimed_by = NULL, lease_until = NULL, claim_unit = NULL,
                    claim_invocation_id = NULL, error_code = NEW.outcome_code,
                    updated_at = clock_timestamp()
                 WHERE job_id = NEW.score_job_id AND run_id = NEW.run_id
                   AND experiment_id = NEW.experiment_id AND evaluation_kind = NEW.evaluation_kind
                   AND task_id = NEW.task_id AND seed = NEW.seed
                   AND candidate_sha256 = NEW.candidate_sha256 AND state = 'running'
                   AND claimed_by = NEW.claim_token
                   AND (NEW.recovery_invocation_id IS NOT NULL OR
                        lease_until > clock_timestamp())
                   AND claim_unit = 'swapp-ai-scientist-scorer-' ||
                       replace(NEW.score_job_id::text, '-', '') || '.service'
                   AND claim_invocation_id = NEW.worker_invocation_id;
                GET DIAGNOSTICS changed = ROW_COUNT;
                IF changed <> 1 THEN
                    RAISE EXCEPTION 'Scorer job claim or invocation is no longer current';
                END IF;
            ELSIF NEW.score_job_id IS NOT NULL THEN
                IF NEW.outcome_code <> 'cancelled' OR NEW.claim_token IS NOT NULL OR
                   NEW.worker_invocation_id IS NOT NULL OR
                   NEW.recovery_invocation_id IS NOT NULL THEN
                    RAISE EXCEPTION 'Planner may attach only queued cancellation jobs';
                END IF;
                IF queued_job IS DISTINCT FROM NEW.score_job_id OR queued_state <> 'queued' THEN
                    RAISE EXCEPTION 'Planner may cancel only the task current queued job';
                END IF;
                UPDATE scorer.score_jobs SET state = 'cancelled', claimed_by = NULL,
                    lease_until = NULL, claim_unit = NULL, claim_invocation_id = NULL,
                    error_code = 'cancelled', updated_at = clock_timestamp()
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
            ELSIF NEW.claim_token IS NOT NULL OR NEW.worker_invocation_id IS NOT NULL OR
                  NEW.recovery_invocation_id IS NOT NULL THEN
                RAISE EXCEPTION 'job claim identity supplied without a score job';
            END IF;
            NEW.claim_token := NULL;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION scorer.guard_terminal_task_invocation() FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION scorer.capture_terminal_task_completion_v9() FROM PUBLIC"
    )


def downgrade() -> None:
    """Restore the 0009 terminal-fence implementation."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION scorer.guard_terminal_task_invocation()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE run_state text; claim scorer.score_jobs%ROWTYPE;
        BEGIN
            IF session_user = 'swapp_lab_planner' THEN
                RETURN NEW;
            END IF;
            IF session_user <> 'swapp_lab_scorer' OR NEW.score_job_id IS NULL OR
               NEW.claim_token IS NULL OR NEW.worker_invocation_id IS NULL OR
               NEW.worker_invocation_id !~ '^[0-9a-f]{32}$' THEN
                RAISE EXCEPTION 'Scorer terminal outcome requires a bound job invocation';
            END IF;
            SELECT state INTO run_state FROM lab.runs
             WHERE run_id = NEW.run_id FOR UPDATE;
            IF NOT FOUND OR
               (NEW.outcome_code = 'cancelled' AND run_state IS DISTINCT FROM 'stop_requested') OR
               (NEW.outcome_code <> 'cancelled' AND run_state IS DISTINCT FROM 'running') THEN
                RAISE EXCEPTION 'terminal outcome does not match the run state';
            END IF;
            SELECT * INTO claim FROM scorer.score_jobs
             WHERE job_id = NEW.score_job_id FOR UPDATE;
            IF NOT FOUND OR claim.state IS DISTINCT FROM 'running' OR
               claim.claimed_by IS DISTINCT FROM NEW.claim_token OR
               claim.lease_until IS NULL OR claim.lease_until <= clock_timestamp() OR
               claim.claim_invocation_id IS DISTINCT FROM NEW.worker_invocation_id OR
               claim.claim_unit IS DISTINCT FROM 'swapp-ai-scientist-scorer-' ||
                    replace(claim.job_id::text, '-', '') || '.service' OR
               claim.run_id IS DISTINCT FROM NEW.run_id OR
               claim.experiment_id IS DISTINCT FROM NEW.experiment_id OR
               claim.evaluation_kind IS DISTINCT FROM NEW.evaluation_kind OR
               claim.task_id IS DISTINCT FROM NEW.task_id OR
               claim.seed IS DISTINCT FROM NEW.seed OR
               claim.candidate_sha256 IS DISTINCT FROM NEW.candidate_sha256 THEN
                RAISE EXCEPTION 'terminal result has no current matching Scorer invocation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION scorer.capture_terminal_task_completion_v9()
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
                UPDATE scorer.score_jobs SET state = CASE WHEN NEW.outcome_code = 'cancelled'
                        THEN 'cancelled' ELSE 'failed' END,
                    claimed_by = NULL, lease_until = NULL, claim_unit = NULL,
                    claim_invocation_id = NULL, error_code = NEW.outcome_code,
                    updated_at = clock_timestamp()
                 WHERE job_id = NEW.score_job_id AND run_id = NEW.run_id
                   AND experiment_id = NEW.experiment_id AND evaluation_kind = NEW.evaluation_kind
                   AND task_id = NEW.task_id AND seed = NEW.seed
                   AND candidate_sha256 = NEW.candidate_sha256 AND state = 'running'
                   AND claimed_by = NEW.claim_token AND lease_until > clock_timestamp()
                   AND claim_unit = 'swapp-ai-scientist-scorer-' ||
                       replace(NEW.score_job_id::text, '-', '') || '.service'
                   AND claim_invocation_id = NEW.worker_invocation_id;
                GET DIAGNOSTICS changed = ROW_COUNT;
                IF changed <> 1 THEN
                    RAISE EXCEPTION 'Scorer job claim or invocation is no longer current';
                END IF;
            ELSIF NEW.score_job_id IS NOT NULL THEN
                IF NEW.outcome_code <> 'cancelled' OR NEW.claim_token IS NOT NULL OR
                   NEW.worker_invocation_id IS NOT NULL THEN
                    RAISE EXCEPTION 'Planner may attach only queued cancellation jobs';
                END IF;
                IF queued_job IS DISTINCT FROM NEW.score_job_id OR queued_state <> 'queued' THEN
                    RAISE EXCEPTION 'Planner may cancel only the task current queued job';
                END IF;
                UPDATE scorer.score_jobs SET state = 'cancelled', claimed_by = NULL,
                    lease_until = NULL, claim_unit = NULL, claim_invocation_id = NULL,
                    error_code = 'cancelled', updated_at = clock_timestamp()
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
            ELSIF NEW.claim_token IS NOT NULL OR NEW.worker_invocation_id IS NOT NULL THEN
                RAISE EXCEPTION 'job claim identity supplied without a score job';
            END IF;
            NEW.claim_token := NULL;
            RETURN NEW;
        END;
        $$
        """
    )
    # Restoring the old function also restores the old rule in the job update:
    # an expired claim cannot be cancelled until the recovery path is re-applied.
    op.execute(
        "REVOKE ALL ON FUNCTION scorer.guard_terminal_task_invocation() FROM PUBLIC"
    )
    op.drop_constraint(
        "ck_task_terminal_recovery_invocation", "task_terminal_outcomes", schema="scorer"
    )
    op.drop_column("task_terminal_outcomes", "recovery_invocation_id", schema="scorer")
