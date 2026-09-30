"""Require terminal experiment records before publishing terminal run reports."""

from __future__ import annotations

from alembic import op

revision = "0012_experiment_report_fence"
down_revision = "0011_experiment_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Fence terminal reports and permit atomic terminal docs after task-plan seal."""
    op.execute(
        """
        CREATE FUNCTION lab.experiment_records_complete(p_run_id uuid)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'experiment completion check requires the Scorer role';
            END IF;
            RETURN NOT EXISTS (
                SELECT 1 FROM lab.experiments e
                 WHERE e.run_id = p_run_id
                   AND (e.status NOT IN ('scored','crashed','abandoned','rejected') OR
                        NOT EXISTS (
                            SELECT 1 FROM lab.experiment_records r
                             WHERE r.experiment_id = e.experiment_id
                        ) OR
                        NOT EXISTS (
                            SELECT 1 FROM lab.trajectory_records t
                             WHERE t.experiment_id = e.experiment_id
                        ))
            );
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION lab.experiment_records_complete(uuid) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.experiment_records_complete(uuid) TO swapp_lab_scorer"
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_run_terminal_report()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        BEGIN
            IF NEW.state IN ('completed','stopped','failed') AND
               OLD.state IS DISTINCT FROM NEW.state AND EXISTS (
                    SELECT 1 FROM lab.experiments e
                     WHERE e.run_id = NEW.run_id
                       AND (e.status NOT IN ('scored','crashed','abandoned','rejected') OR
                            NOT EXISTS (
                                SELECT 1 FROM lab.experiment_records r
                                 WHERE r.experiment_id = e.experiment_id
                            ) OR
                            NOT EXISTS (
                                SELECT 1 FROM lab.trajectory_records t
                                 WHERE t.experiment_id = e.experiment_id
                            ))
               ) THEN
                RAISE EXCEPTION 'run report requires terminal experiment and trajectory records';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION lab.guard_run_terminal_report() FROM PUBLIC")
    op.execute(
        """
        CREATE TRIGGER trg_run_terminal_report_requires_experiment_records
        BEFORE UPDATE OF state ON lab.runs
        FOR EACH ROW EXECUTE FUNCTION lab.guard_run_terminal_report()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION lab.guard_experiment_lifecycle()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE current_run lab.runs%ROWTYPE; task_count bigint; score_count bigint;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'experiment lifecycle requires the Director role';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'proposed' THEN
                    RAISE EXCEPTION 'new experiments must start proposed';
                END IF;
                SELECT * INTO current_run FROM lab.runs
                 WHERE run_id = NEW.run_id FOR UPDATE;
                IF NOT FOUND OR current_run.state <> 'running' OR
                   current_run.task_plan_sha256 IS NOT NULL THEN
                    RAISE EXCEPTION 'experiment proposal requires an open running run';
                END IF;
                IF NEW.parent_experiment_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM lab.experiments parent
                     WHERE parent.experiment_id = NEW.parent_experiment_id
                       AND parent.run_id = NEW.run_id
                ) THEN
                    RAISE EXCEPTION 'parent experiment must belong to the same run';
                END IF;
                RETURN NEW;
            END IF;
            IF (NEW.experiment_id, NEW.run_id, NEW.sequence, NEW.experiment_number,
                NEW.kind, NEW.baseline_name, NEW.parent_experiment_id,
                NEW.candidate_sha256, NEW.candidate_blob_sha256, NEW.inputs_sha256,
                NEW.move_type, NEW.system, NEW.hypothesis, NEW.predicted_delta,
                NEW.proposal_json, NEW.created_at)
               IS DISTINCT FROM
               (OLD.experiment_id, OLD.run_id, OLD.sequence, OLD.experiment_number,
                OLD.kind, OLD.baseline_name, OLD.parent_experiment_id,
                OLD.candidate_sha256, OLD.candidate_blob_sha256, OLD.inputs_sha256,
                OLD.move_type, OLD.system, OLD.hypothesis, OLD.predicted_delta,
                OLD.proposal_json, OLD.created_at) THEN
                RAISE EXCEPTION 'experiment proposal identity is immutable';
            END IF;
            SELECT * INTO current_run FROM lab.runs
             WHERE run_id = NEW.run_id FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'experiment run does not exist';
            END IF;
            IF current_run.state = 'running' THEN
                IF current_run.task_plan_sha256 IS NOT NULL AND
                   NEW.status NOT IN ('scored','crashed','abandoned','rejected') THEN
                    RAISE EXCEPTION 'sealed plan permits terminal document commits only';
                END IF;
            ELSIF current_run.state = 'stop_requested' THEN
                IF NEW.status <> 'abandoned' THEN
                    RAISE EXCEPTION 'stopped run experiments may only be abandoned';
                END IF;
            ELSE
                RAISE EXCEPTION 'experiment lifecycle requires a nonterminal run';
            END IF;
            IF NOT (
                (OLD.status = 'proposed' AND NEW.status IN
                    ('primary_running','rejected','crashed','abandoned')) OR
                (OLD.status = 'primary_running' AND NEW.status IN
                    ('awaiting_confirmation','scored','rejected','crashed','abandoned')) OR
                (OLD.status = 'awaiting_confirmation' AND NEW.status IN
                    ('confirmation_running','rejected','crashed','abandoned')) OR
                (OLD.status = 'confirmation_running' AND NEW.status IN
                    ('scored','rejected','crashed','abandoned'))
            ) THEN
                RAISE EXCEPTION 'invalid experiment status transition';
            END IF;
            IF NEW.status = 'scored' THEN
                SELECT count(*) INTO task_count FROM scorer.run_tasks
                 WHERE run_id = NEW.run_id AND experiment_id = NEW.experiment_id;
                SELECT count(*) INTO score_count FROM scorer.task_scores
                 WHERE run_id = NEW.run_id AND experiment_id = NEW.experiment_id;
                IF task_count = 0 OR score_count <> task_count THEN
                    RAISE EXCEPTION 'scored experiment requires scores for every planned task';
                END IF;
            END IF;
            NEW.updated_at := clock_timestamp();
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION lab.guard_experiment_lifecycle() FROM PUBLIC")


def downgrade() -> None:
    """Remove terminal-run fence and restore the prior experiment lifecycle guard."""
    op.execute("DROP TRIGGER trg_run_terminal_report_requires_experiment_records ON lab.runs")
    op.execute("DROP FUNCTION lab.guard_run_terminal_report()")
    op.execute("DROP FUNCTION lab.experiment_records_complete(uuid)")
    # The 0011 trigger is restored by reusing its original implementation on downgrade.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION lab.guard_experiment_lifecycle()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE current_run lab.runs%ROWTYPE; task_count bigint; score_count bigint;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'experiment lifecycle requires the Director role';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'proposed' THEN
                    RAISE EXCEPTION 'new experiments must start proposed';
                END IF;
                SELECT * INTO current_run FROM lab.runs
                 WHERE run_id = NEW.run_id FOR UPDATE;
                IF NOT FOUND OR current_run.state <> 'running' OR
                   current_run.task_plan_sha256 IS NOT NULL THEN
                    RAISE EXCEPTION 'experiment proposal requires an open running run';
                END IF;
                IF NEW.parent_experiment_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM lab.experiments parent
                     WHERE parent.experiment_id = NEW.parent_experiment_id
                       AND parent.run_id = NEW.run_id
                ) THEN
                    RAISE EXCEPTION 'parent experiment must belong to the same run';
                END IF;
                RETURN NEW;
            END IF;
            IF (NEW.experiment_id, NEW.run_id, NEW.sequence, NEW.experiment_number,
                NEW.kind, NEW.baseline_name, NEW.parent_experiment_id,
                NEW.candidate_sha256, NEW.candidate_blob_sha256, NEW.inputs_sha256,
                NEW.move_type, NEW.system, NEW.hypothesis, NEW.predicted_delta,
                NEW.proposal_json, NEW.created_at)
               IS DISTINCT FROM
               (OLD.experiment_id, OLD.run_id, OLD.sequence, OLD.experiment_number,
                OLD.kind, OLD.baseline_name, OLD.parent_experiment_id,
                OLD.candidate_sha256, OLD.candidate_blob_sha256, OLD.inputs_sha256,
                OLD.move_type, OLD.system, OLD.hypothesis, OLD.predicted_delta,
                OLD.proposal_json, OLD.created_at) THEN
                RAISE EXCEPTION 'experiment proposal identity is immutable';
            END IF;
            SELECT * INTO current_run FROM lab.runs
             WHERE run_id = NEW.run_id FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'experiment run does not exist';
            END IF;
            IF current_run.state <> 'running' OR current_run.task_plan_sha256 IS NOT NULL THEN
                IF NOT (current_run.state = 'stop_requested' AND NEW.status = 'abandoned') THEN
                    RAISE EXCEPTION 'experiment lifecycle requires an open running run';
                END IF;
            END IF;
            IF NOT (
                (OLD.status = 'proposed' AND NEW.status IN
                    ('primary_running','rejected','crashed','abandoned')) OR
                (OLD.status = 'primary_running' AND NEW.status IN
                    ('awaiting_confirmation','scored','rejected','crashed','abandoned')) OR
                (OLD.status = 'awaiting_confirmation' AND NEW.status IN
                    ('confirmation_running','rejected','crashed','abandoned')) OR
                (OLD.status = 'confirmation_running' AND NEW.status IN
                    ('scored','rejected','crashed','abandoned'))
            ) THEN
                RAISE EXCEPTION 'invalid experiment status transition';
            END IF;
            IF NEW.status = 'scored' THEN
                SELECT count(*) INTO task_count FROM scorer.run_tasks
                 WHERE run_id = NEW.run_id AND experiment_id = NEW.experiment_id;
                SELECT count(*) INTO score_count FROM scorer.task_scores
                 WHERE run_id = NEW.run_id AND experiment_id = NEW.experiment_id;
                IF task_count = 0 OR score_count <> task_count THEN
                    RAISE EXCEPTION 'scored experiment requires scores for every planned task';
                END IF;
            END IF;
            NEW.updated_at := clock_timestamp();
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION lab.guard_experiment_lifecycle() FROM PUBLIC")
