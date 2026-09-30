"""Bind durable Scorer writes to a verified systemd unit invocation."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_scorer_invocation_fence"
down_revision = "0008_terminal_task_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Persist claim generation and enforce it for score/terminal writes."""
    # An existing running claim predates systemd invocation binding. It cannot
    # be upgraded safely by guessing which process generation owns it.
    op.execute(
        """
        DO $$
        DECLARE active_claims bigint;
        BEGIN
            SELECT count(*) INTO active_claims FROM scorer.score_jobs WHERE state = 'running';
            IF active_claims <> 0 THEN
                RAISE EXCEPTION
                    '0009 requires zero legacy running score jobs; found %', active_claims;
            END IF;
        END;
        $$
        """
    )
    op.add_column("score_jobs", sa.Column("claim_unit", sa.String(128)), schema="scorer")
    op.add_column("score_jobs", sa.Column("claim_invocation_id", sa.String(32)), schema="scorer")
    op.add_column(
        "task_scores",
        sa.Column("score_job_id", sa.Uuid(as_uuid=True)),
        schema="scorer",
    )
    op.add_column("task_scores", sa.Column("claim_token", sa.String(128)), schema="scorer")
    op.create_foreign_key(
        "fk_task_scores_score_job_id_score_jobs",
        "task_scores",
        "score_jobs",
        ["score_job_id"],
        ["job_id"],
        source_schema="scorer",
        referent_schema="scorer",
    )
    op.add_column(
        "task_scores", sa.Column("worker_invocation_id", sa.String(32)), schema="scorer"
    )
    op.add_column(
        "task_terminal_outcomes",
        sa.Column("worker_invocation_id", sa.String(32)),
        schema="scorer",
    )
    op.execute(
        """
        CREATE FUNCTION scorer.guard_score_job_invocation()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE completion text;
        BEGIN
            IF NEW.state = 'running' THEN
                IF session_user <> 'swapp_lab_scorer' OR NEW.claimed_by IS NULL OR
                   NEW.lease_until IS NULL OR NEW.lease_until <= clock_timestamp() OR
                   NEW.claim_invocation_id IS NULL OR
                   NEW.claim_invocation_id !~ '^[0-9a-f]{32}$' OR
                   NEW.claim_unit IS NULL OR
                   NEW.claim_unit <> 'swapp-ai-scientist-scorer-' ||
                       replace(NEW.job_id::text, '-', '') || '.service' THEN
                    RAISE EXCEPTION 'Scorer claim requires the exact owned unit invocation';
                END IF;
            ELSE
                IF NEW.claimed_by IS NOT NULL OR NEW.lease_until IS NOT NULL THEN
                    RAISE EXCEPTION 'non-running score job cannot retain a token or lease';
                END IF;
                IF (NEW.claim_unit IS NULL) <> (NEW.claim_invocation_id IS NULL) THEN
                    RAISE EXCEPTION 'claim unit and invocation must be cleared together';
                END IF;
                IF NEW.claim_unit IS NOT NULL AND
                   (OLD.state <> 'running' OR session_user <> 'swapp_lab_scorer' OR
                    NEW.claim_unit IS DISTINCT FROM OLD.claim_unit OR
                    NEW.claim_invocation_id IS DISTINCT FROM OLD.claim_invocation_id) THEN
                    RAISE EXCEPTION 'only the current Scorer may finish its running claim';
                END IF;
                IF OLD.state = 'running' AND NEW.state IN ('completed','failed','cancelled') THEN
                    SELECT completion_kind INTO completion FROM scorer.task_completions
                     WHERE run_id = OLD.run_id AND experiment_id = OLD.experiment_id
                       AND evaluation_kind = OLD.evaluation_kind AND task_id = OLD.task_id
                       AND seed = OLD.seed;
                    IF session_user <> 'swapp_lab_scorer' OR
                       completion IS DISTINCT FROM (CASE NEW.state
                           WHEN 'completed' THEN 'scored' ELSE 'terminal' END) THEN
                        RAISE EXCEPTION
                            'running claim can finish only with its durable task result';
                    END IF;
                ELSIF OLD.state = 'running' AND NEW.state = 'queued' THEN
                    IF session_user <> 'swapp_lab_scorer' OR OLD.claimed_by IS NULL OR
                       OLD.claim_unit IS NULL OR OLD.claim_invocation_id IS NULL THEN
                        RAISE EXCEPTION 'only an owned Scorer claim can be retried';
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
        CREATE TRIGGER trg_score_job_invocation_identity
        BEFORE UPDATE ON scorer.score_jobs
        FOR EACH ROW EXECUTE FUNCTION scorer.guard_score_job_invocation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION scorer.guard_task_score_invocation()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE run_state text; claim scorer.score_jobs%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'task score requires the Scorer role';
            END IF;
            IF NEW.score_job_id IS NULL OR NEW.claim_token IS NULL OR
               NEW.worker_invocation_id IS NULL OR
               NEW.worker_invocation_id !~ '^[0-9a-f]{32}$' THEN
                RAISE EXCEPTION 'task score requires a bound systemd claim';
            END IF;
            SELECT state INTO run_state FROM lab.runs
             WHERE run_id = NEW.run_id FOR UPDATE;
            IF NOT FOUND OR run_state IS DISTINCT FROM 'running' THEN
                RAISE EXCEPTION 'task score requires a running Lab run';
            END IF;
            SELECT * INTO claim FROM scorer.score_jobs
             WHERE job_id = NEW.score_job_id FOR UPDATE;
            IF NOT FOUND OR claim.state IS DISTINCT FROM 'running' OR
               claim.lease_until IS NULL OR claim.lease_until <= clock_timestamp() OR
               claim.claimed_by IS DISTINCT FROM NEW.claim_token OR
               claim.claim_invocation_id IS DISTINCT FROM NEW.worker_invocation_id OR
               claim.claim_unit IS DISTINCT FROM 'swapp-ai-scientist-scorer-' ||
                    replace(claim.job_id::text, '-', '') || '.service' OR
               (claim.run_id, claim.experiment_id, claim.evaluation_kind, claim.task_id, claim.seed)
                   IS DISTINCT FROM
               (NEW.run_id, NEW.experiment_id, NEW.evaluation_kind, NEW.task_id, NEW.seed) OR
               claim.candidate_sha256 IS DISTINCT FROM NEW.score->>'candidate_sha256' OR
               claim.artifact_sha256 IS DISTINCT FROM NEW.score->>'candidate_output_sha256' THEN
                RAISE EXCEPTION 'task score has no current matching Scorer invocation';
            END IF;
            NEW.claim_token := NULL;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER aaa_task_score_invocation_identity
        BEFORE INSERT ON scorer.task_scores
        FOR EACH ROW EXECUTE FUNCTION scorer.guard_task_score_invocation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION scorer.finish_task_score_job()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE changed integer;
        BEGIN
            UPDATE scorer.score_jobs SET state = 'completed', claimed_by = NULL,
                lease_until = NULL, claim_unit = NULL, claim_invocation_id = NULL,
                error_code = NULL, updated_at = clock_timestamp()
             WHERE job_id = NEW.score_job_id AND state = 'running'
               AND claim_invocation_id = NEW.worker_invocation_id;
            GET DIAGNOSTICS changed = ROW_COUNT;
            IF changed <> 1 THEN
                RAISE EXCEPTION 'Scorer invocation changed before score completion';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER zzz_finish_task_score_job
        AFTER INSERT ON scorer.task_scores
        FOR EACH ROW EXECUTE FUNCTION scorer.finish_task_score_job()
        """
    )
    op.execute(
        """
        CREATE FUNCTION scorer.guard_terminal_task_invocation()
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
        CREATE TRIGGER aaa_terminal_task_invocation_identity
        BEFORE INSERT ON scorer.task_terminal_outcomes
        FOR EACH ROW EXECUTE FUNCTION scorer.guard_terminal_task_invocation()
        """
    )
    op.execute(
        """
        DROP TRIGGER trg_terminal_task_completion ON scorer.task_terminal_outcomes;
        CREATE FUNCTION scorer.capture_terminal_task_completion_v9()
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
    for function in (
        "guard_score_job_invocation",
        "guard_task_score_invocation",
        "finish_task_score_job",
        "guard_terminal_task_invocation",
        "capture_terminal_task_completion_v9",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION scorer.{function}() FROM PUBLIC")
    op.execute(
        """
        CREATE TRIGGER trg_terminal_task_completion_v9
        BEFORE INSERT ON scorer.task_terminal_outcomes
        FOR EACH ROW EXECUTE FUNCTION scorer.capture_terminal_task_completion_v9()
        """
    )


def downgrade() -> None:
    """Remove invocation fences, retaining score and terminal results."""
    op.execute("DROP TRIGGER trg_terminal_task_completion_v9 ON scorer.task_terminal_outcomes")
    op.execute(
        """
        CREATE TRIGGER trg_terminal_task_completion
        BEFORE INSERT ON scorer.task_terminal_outcomes
        FOR EACH ROW EXECUTE FUNCTION scorer.capture_terminal_task_completion()
        """
    )
    op.execute(
        "DROP TRIGGER aaa_terminal_task_invocation_identity ON scorer.task_terminal_outcomes"
    )
    op.execute("DROP TRIGGER zzz_finish_task_score_job ON scorer.task_scores")
    op.execute("DROP TRIGGER aaa_task_score_invocation_identity ON scorer.task_scores")
    op.execute("DROP TRIGGER trg_score_job_invocation_identity ON scorer.score_jobs")
    for function in (
        "capture_terminal_task_completion_v9",
        "guard_terminal_task_invocation",
        "finish_task_score_job",
        "guard_task_score_invocation",
        "guard_score_job_invocation",
    ):
        op.execute(f"DROP FUNCTION scorer.{function}()")
    op.drop_column("task_terminal_outcomes", "worker_invocation_id", schema="scorer")
    op.drop_column("task_scores", "worker_invocation_id", schema="scorer")
    op.drop_constraint(
        "fk_task_scores_score_job_id_score_jobs", "task_scores", schema="scorer", type_="foreignkey"
    )
    op.drop_column("task_scores", "claim_token", schema="scorer")
    op.drop_column("task_scores", "score_job_id", schema="scorer")
    op.drop_column("score_jobs", "claim_invocation_id", schema="scorer")
    op.drop_column("score_jobs", "claim_unit", schema="scorer")
