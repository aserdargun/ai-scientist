"""Persist experiment lifecycle, immutable records, trajectories, and dev-only results."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_experiment_ledger"
down_revision = "0010_terminal_recovery_fence"
branch_labels = None
depends_on = None

REGISTER_SIGNATURE = (
    "lab.register_experiment(text,uuid,integer,integer,text,text,text,text,text,text,"
    "text,text,text,double precision,jsonb)"
)
COMMIT_SIGNATURE = "lab.commit_experiment_documents(text,jsonb,text,text,jsonb,text,text,text)"


def upgrade() -> None:
    """Add append-only experiment documents and a label-free dev result surface."""
    op.add_column(
        "dataset_profiles",
        sa.Column("visibility", sa.String(length=16), server_default="sealed", nullable=False),
        schema="scorer",
    )
    op.create_check_constraint(
        "ck_dataset_profile_visibility",
        "dataset_profiles",
        "visibility in ('dev','holdout','sealed')",
        schema="scorer",
    )
    op.create_table(
        "experiments",
        sa.Column("experiment_id", sa.String(length=128), primary_key=True),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("experiment_number", sa.Integer()),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("baseline_name", sa.String(length=32)),
        sa.Column("parent_experiment_id", sa.String(length=128)),
        sa.Column("candidate_sha256", sa.String(length=64), nullable=False),
        sa.Column("candidate_blob_sha256", sa.String(length=64), nullable=False),
        sa.Column("inputs_sha256", sa.String(length=64), nullable=False),
        sa.Column("move_type", sa.String(length=32), nullable=False),
        sa.Column("system", sa.String(length=2), nullable=False),
        sa.Column("hypothesis", sa.String(length=2000), nullable=False),
        sa.Column("predicted_delta", sa.Float()),
        sa.Column("proposal_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="proposed", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["run_id"], ["lab.runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_experiment_id"], ["lab.experiments.experiment_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("run_id", "sequence", name="uq_experiments_run_sequence"),
        sa.UniqueConstraint("run_id", "experiment_number", name="uq_experiments_run_number"),
        sa.CheckConstraint("sequence >= 0", name="ck_experiments_sequence"),
        sa.CheckConstraint(
            "(kind = 'baseline' and experiment_number is null and baseline_name is not null "
            "and baseline_name in "
            "('robust_z','iforest','ecod_train_frozen')) or "
            "(kind = 'proposal' and experiment_number is not null and experiment_number >= 1 "
            "and baseline_name is null)",
            name="ck_experiments_kind_identity",
        ),
        sa.CheckConstraint(
            "candidate_sha256 ~ '^[0-9a-f]{64}$'", name="ck_experiments_candidate_sha"
        ),
        sa.CheckConstraint(
            "candidate_blob_sha256 ~ '^[0-9a-f]{64}$'", name="ck_experiments_blob_sha"
        ),
        sa.CheckConstraint("inputs_sha256 ~ '^[0-9a-f]{64}$'", name="ck_experiments_inputs_sha"),
        sa.CheckConstraint(
            "predicted_delta is null or predicted_delta between -4 and 4",
            name="ck_experiments_predicted_delta",
        ),
        sa.CheckConstraint("system in ('S1','S2')", name="ck_experiments_system"),
        sa.CheckConstraint(
            "status in ('proposed','primary_running','awaiting_confirmation',"
            "'confirmation_running','scored','crashed','abandoned','rejected')",
            name="ck_experiments_status",
        ),
        schema="lab",
    )
    op.create_table(
        "experiment_records",
        sa.Column("experiment_id", sa.String(length=128), primary_key=True),
        sa.Column("experiment_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("experiment_sha256", sa.String(length=64), nullable=False),
        sa.Column("experiment_blob_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["lab.experiments.experiment_id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("experiment_sha256 ~ '^[0-9a-f]{64}$'", name="ck_experiment_record_sha"),
        sa.CheckConstraint(
            "experiment_blob_sha256 ~ '^[0-9a-f]{64}$'", name="ck_experiment_record_blob_sha"
        ),
        schema="lab",
    )
    op.create_table(
        "trajectory_records",
        sa.Column("experiment_id", sa.String(length=128), primary_key=True),
        sa.Column("trajectory_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trajectory_sha256", sa.String(length=64), nullable=False),
        sa.Column("trajectory_blob_sha256", sa.String(length=64), nullable=False),
        sa.Column("messages_blob_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["lab.experiments.experiment_id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("trajectory_sha256 ~ '^[0-9a-f]{64}$'", name="ck_trajectory_record_sha"),
        sa.CheckConstraint(
            "trajectory_blob_sha256 ~ '^[0-9a-f]{64}$'", name="ck_trajectory_record_blob_sha"
        ),
        sa.CheckConstraint(
            "messages_blob_sha256 ~ '^[0-9a-f]{64}$'", name="ck_trajectory_messages_blob_sha"
        ),
        schema="lab",
    )
    op.execute(
        """
        CREATE FUNCTION scorer.guard_dataset_visibility_immutable()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, scorer AS $$
        BEGIN
            IF NEW.visibility IS DISTINCT FROM OLD.visibility THEN
                RAISE EXCEPTION 'dataset visibility is immutable; publish a new profile revision';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_dataset_visibility_immutable
        BEFORE UPDATE OF visibility ON scorer.dataset_profiles
        FOR EACH ROW EXECUTE FUNCTION scorer.guard_dataset_visibility_immutable()
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.lock_run_plan(p_run_id uuid)
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
    op.execute(
        """
        CREATE FUNCTION lab.guard_experiment_lifecycle()
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
    op.execute(
        """
        CREATE TRIGGER trg_experiment_lifecycle
        BEFORE INSERT OR UPDATE ON lab.experiments
        FOR EACH ROW EXECUTE FUNCTION lab.guard_experiment_lifecycle()
        """
    )
    op.execute(
        """
        CREATE FUNCTION scorer.guard_experiment_task_identity()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE experiment_row lab.experiments%ROWTYPE;
        BEGIN
            SELECT * INTO experiment_row FROM lab.experiments
             WHERE experiment_id = NEW.experiment_id AND run_id = NEW.run_id;
            IF NOT FOUND OR experiment_row.candidate_sha256 IS DISTINCT FROM NEW.candidate_sha256 OR
               experiment_row.status NOT IN ('proposed','primary_running',
                    'awaiting_confirmation','confirmation_running') THEN
                RAISE EXCEPTION 'task assignment does not match an active experiment';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_experiment_task_identity
        BEFORE INSERT ON scorer.run_tasks
        FOR EACH ROW EXECUTE FUNCTION scorer.guard_experiment_task_identity()
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_experiment_record_insert()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE exp lab.experiments%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'experiment record requires the Director role';
            END IF;
            SELECT * INTO exp FROM lab.experiments
             WHERE experiment_id = NEW.experiment_id FOR UPDATE;
            IF NOT FOUND OR exp.status NOT IN ('scored','crashed','abandoned','rejected') OR
               NEW.experiment_json->>'schema' IS DISTINCT FROM 'experiment.v1' OR
               NEW.experiment_json->>'experiment_id' IS DISTINCT FROM exp.experiment_id::text OR
               NEW.experiment_json->>'run_id' IS DISTINCT FROM exp.run_id::text OR
               NEW.experiment_json->>'candidate_sha256' IS DISTINCT FROM exp.candidate_sha256 OR
               NEW.experiment_json->>'candidate_blob_sha256' IS DISTINCT FROM
                    exp.candidate_blob_sha256 OR
               NEW.experiment_json->>'inputs_sha256' IS DISTINCT FROM exp.inputs_sha256 OR
               (NEW.experiment_json->>'ordinal')::integer IS DISTINCT FROM exp.sequence + 1 OR
               NEW.experiment_json->>'parent_experiment_id' IS DISTINCT FROM
                    exp.parent_experiment_id OR
               NEW.experiment_json->>'move_type' IS DISTINCT FROM exp.move_type OR
               NEW.experiment_json->>'system' IS DISTINCT FROM exp.system OR
               NEW.experiment_json->>'hypothesis' IS DISTINCT FROM exp.hypothesis OR
               NEW.experiment_json->'predicted_delta' IS DISTINCT FROM
                    coalesce(to_jsonb(exp.predicted_delta), 'null'::jsonb) OR
               NEW.experiment_json->>'status' IS DISTINCT FROM exp.status THEN
                RAISE EXCEPTION 'experiment record does not match its terminal proposal';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_experiment_record_insert
        BEFORE INSERT ON lab.experiment_records
        FOR EACH ROW EXECUTE FUNCTION lab.guard_experiment_record_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_trajectory_record_insert()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE exp lab.experiments%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_director' OR NOT EXISTS (
                SELECT 1 FROM lab.experiment_records rec
                 WHERE rec.experiment_id = NEW.experiment_id
            ) OR NEW.trajectory_json->>'schema' IS DISTINCT FROM 'trajectory.v1' OR
               NEW.trajectory_json->>'experiment_id' IS DISTINCT FROM NEW.experiment_id::text THEN
                RAISE EXCEPTION 'trajectory requires its terminal experiment record';
            END IF;
            SELECT * INTO exp FROM lab.experiments WHERE experiment_id = NEW.experiment_id;
            IF NOT FOUND OR NEW.trajectory_json->>'run_id' IS DISTINCT FROM exp.run_id::text OR
               NEW.trajectory_json->>'system' IS DISTINCT FROM exp.system OR
               NEW.trajectory_json->>'inputs_sha256' IS DISTINCT FROM exp.inputs_sha256 OR
               NEW.trajectory_json->>'agent_version' IS DISTINCT FROM
                    (SELECT rec.experiment_json->>'agent_version'
                       FROM lab.experiment_records rec
                      WHERE rec.experiment_id = NEW.experiment_id) OR
               NEW.trajectory_json->'outcome' IS DISTINCT FROM
                    (SELECT rec.experiment_json->'decision'
                       FROM lab.experiment_records rec
                      WHERE rec.experiment_id = NEW.experiment_id) OR
               NEW.trajectory_json->>'messages_blob_sha256' IS DISTINCT FROM
                    NEW.messages_blob_sha256 OR
               NEW.trajectory_sha256 !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'trajectory identity does not match its experiment';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_trajectory_record_insert
        BEFORE INSERT ON lab.trajectory_records
        FOR EACH ROW EXECUTE FUNCTION lab.guard_trajectory_record_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.reject_immutable_document_mutation()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND session_user = 'swapp_lab_migrator' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'experiment and trajectory documents are immutable';
        END;
        $$
        """
    )
    for table in ("experiment_records", "trajectory_records"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_immutable
            BEFORE UPDATE OR DELETE ON lab.{table}
            FOR EACH ROW EXECUTE FUNCTION lab.reject_immutable_document_mutation()
            """
        )
    op.execute(
        """
        CREATE FUNCTION lab.register_experiment(
            p_experiment_id text, p_run_id uuid, p_sequence integer, p_experiment_number integer,
            p_kind text, p_baseline_name text, p_parent_experiment_id text,
            p_candidate_sha256 text, p_candidate_blob_sha256 text, p_inputs_sha256 text,
            p_move_type text, p_system text, p_hypothesis text, p_predicted_delta double precision,
            p_proposal_json jsonb
        ) RETURNS lab.experiments LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE current_run lab.runs%ROWTYPE; existing lab.experiments%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'experiment registration requires the Director role';
            END IF;
            PERFORM lab.lock_run_plan(p_run_id);
            SELECT * INTO current_run FROM lab.runs WHERE run_id = p_run_id FOR UPDATE;
            IF NOT FOUND OR current_run.state <> 'running' OR
               current_run.task_plan_sha256 IS NOT NULL THEN
                RAISE EXCEPTION 'experiment proposal requires an open running run';
            END IF;
            SELECT * INTO existing FROM lab.experiments
             WHERE experiment_id = p_experiment_id FOR UPDATE;
            IF FOUND THEN
                IF (existing.run_id, existing.sequence, existing.experiment_number, existing.kind,
                    existing.baseline_name, existing.parent_experiment_id,
                    existing.candidate_sha256,
                    existing.candidate_blob_sha256, existing.inputs_sha256, existing.move_type,
                    existing.system, existing.hypothesis, existing.predicted_delta,
                    existing.proposal_json)
                   IS DISTINCT FROM
                   (p_run_id, p_sequence, p_experiment_number, p_kind, p_baseline_name,
                    p_parent_experiment_id, p_candidate_sha256, p_candidate_blob_sha256,
                    p_inputs_sha256, p_move_type, p_system, p_hypothesis, p_predicted_delta,
                    p_proposal_json) THEN
                    RAISE EXCEPTION 'experiment id already exists with different proposal';
                END IF;
                RETURN existing;
            END IF;
            INSERT INTO lab.experiments(
                experiment_id, run_id, sequence, experiment_number, kind, baseline_name,
                parent_experiment_id, candidate_sha256, candidate_blob_sha256, inputs_sha256,
                move_type, system, hypothesis, predicted_delta, proposal_json
            ) VALUES (
                p_experiment_id, p_run_id, p_sequence, p_experiment_number, p_kind, p_baseline_name,
                p_parent_experiment_id, p_candidate_sha256, p_candidate_blob_sha256,
                p_inputs_sha256,
                p_move_type, p_system, p_hypothesis, p_predicted_delta, p_proposal_json
            ) RETURNING * INTO existing;
            RETURN existing;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.transition_experiment(p_experiment_id text, p_next_status text)
        RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE exp lab.experiments%ROWTYPE; current_run lab.runs%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'experiment transition requires the Director role';
            END IF;
            SELECT * INTO exp FROM lab.experiments WHERE experiment_id = p_experiment_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'unknown experiment'; END IF;
            PERFORM lab.lock_run_plan(exp.run_id);
            SELECT * INTO current_run FROM lab.runs WHERE run_id = exp.run_id FOR UPDATE;
            SELECT * INTO exp FROM lab.experiments WHERE experiment_id = p_experiment_id FOR UPDATE;
            IF exp.status = p_next_status THEN
                RETURN exp.status;
            END IF;
            IF current_run.state = 'stop_requested' AND p_next_status <> 'abandoned' THEN
                RAISE EXCEPTION 'stopped run experiments may only be abandoned';
            END IF;
            IF p_next_status NOT IN ('primary_running','awaiting_confirmation',
                    'confirmation_running') THEN
                RAISE EXCEPTION 'status requires an atomic terminal-document commit';
            END IF;
            UPDATE lab.experiments SET status = p_next_status
             WHERE experiment_id = p_experiment_id;
            RETURN p_next_status;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.commit_experiment_documents(
            p_experiment_id text, p_experiment_json jsonb, p_experiment_sha256 text,
            p_experiment_blob_sha256 text, p_trajectory_json jsonb, p_trajectory_sha256 text,
            p_trajectory_blob_sha256 text, p_messages_blob_sha256 text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE exp lab.experiments%ROWTYPE; current_run lab.runs%ROWTYPE;
            prior_exp lab.experiment_records%ROWTYPE; prior_traj lab.trajectory_records%ROWTYPE;
            target_status text;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'experiment documents require the Director role';
            END IF;
            SELECT * INTO exp FROM lab.experiments WHERE experiment_id = p_experiment_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'unknown experiment'; END IF;
            PERFORM lab.lock_run_plan(exp.run_id);
            SELECT * INTO current_run FROM lab.runs WHERE run_id = exp.run_id FOR UPDATE;
            SELECT * INTO exp FROM lab.experiments WHERE experiment_id = p_experiment_id FOR UPDATE;
            SELECT * INTO prior_exp FROM lab.experiment_records
             WHERE experiment_id = p_experiment_id;
            SELECT * INTO prior_traj FROM lab.trajectory_records
             WHERE experiment_id = p_experiment_id;
            IF prior_exp.experiment_id IS NOT NULL OR prior_traj.experiment_id IS NOT NULL THEN
                IF prior_exp.experiment_json IS DISTINCT FROM p_experiment_json OR
                   prior_exp.experiment_sha256 IS DISTINCT FROM p_experiment_sha256 OR
                   prior_exp.experiment_blob_sha256 IS DISTINCT FROM p_experiment_blob_sha256 OR
                   prior_traj.trajectory_json IS DISTINCT FROM p_trajectory_json OR
                   prior_traj.trajectory_sha256 IS DISTINCT FROM p_trajectory_sha256 OR
                   prior_traj.trajectory_blob_sha256 IS DISTINCT FROM p_trajectory_blob_sha256 OR
                   prior_traj.messages_blob_sha256 IS DISTINCT FROM p_messages_blob_sha256 THEN
                    RAISE EXCEPTION 'experiment terminal replay differs from committed documents';
                END IF;
                RETURN jsonb_build_object(
                    'status','already_committed','experiment_id',p_experiment_id
                );
            END IF;
            IF p_experiment_json->>'schema' IS DISTINCT FROM 'experiment.v1' OR
               p_experiment_json->>'experiment_id' IS DISTINCT FROM exp.experiment_id OR
               p_experiment_json->>'run_id' IS DISTINCT FROM exp.run_id::text OR
               p_experiment_json->>'candidate_sha256' IS DISTINCT FROM exp.candidate_sha256 OR
               p_experiment_json->>'candidate_blob_sha256' IS DISTINCT FROM
                    exp.candidate_blob_sha256 OR
               p_experiment_json->>'inputs_sha256' IS DISTINCT FROM exp.inputs_sha256 OR
               p_experiment_json->>'hypothesis' IS DISTINCT FROM exp.hypothesis OR
               p_experiment_json->>'move_type' IS DISTINCT FROM exp.move_type OR
               p_experiment_json->>'system' IS DISTINCT FROM exp.system OR
               p_experiment_json->>'status' NOT IN ('scored','crashed','abandoned','rejected') OR
               (p_experiment_json->>'ordinal')::integer IS DISTINCT FROM exp.sequence + 1 OR
               p_experiment_json->>'parent_experiment_id' IS DISTINCT FROM
                    exp.parent_experiment_id OR
               p_experiment_json->>'hypothesis' IS DISTINCT FROM exp.hypothesis OR
               p_experiment_json->'predicted_delta' IS DISTINCT FROM
                    coalesce(to_jsonb(exp.predicted_delta), 'null'::jsonb) OR
               p_trajectory_json->'outcome' IS DISTINCT FROM p_experiment_json->'decision' OR
               p_trajectory_json->>'agent_version' IS DISTINCT FROM
                    p_experiment_json->>'agent_version' OR
               p_experiment_sha256 !~ '^[0-9a-f]{64}$' OR
               p_experiment_blob_sha256 !~ '^[0-9a-f]{64}$' OR
               p_trajectory_sha256 !~ '^[0-9a-f]{64}$' OR
               p_trajectory_blob_sha256 !~ '^[0-9a-f]{64}$' OR
               p_messages_blob_sha256 !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'experiment document does not match immutable proposal';
            END IF;
            target_status := p_experiment_json->>'status';
            IF current_run.state NOT IN ('running','stop_requested') THEN
                RAISE EXCEPTION 'terminal experiment cannot commit after run finalization';
            END IF;
            IF target_status <> 'abandoned' AND current_run.state <> 'running' THEN
                RAISE EXCEPTION 'only a running run can commit a non-abandoned experiment';
            END IF;
            IF current_run.state = 'stop_requested' AND target_status <> 'abandoned' THEN
                RAISE EXCEPTION 'stopped run can only commit an abandoned experiment';
            END IF;
            UPDATE lab.experiments SET status = target_status WHERE experiment_id = p_experiment_id;
            INSERT INTO lab.experiment_records(
                experiment_id, experiment_json, experiment_sha256, experiment_blob_sha256
            ) VALUES (p_experiment_id, p_experiment_json, p_experiment_sha256,
                      p_experiment_blob_sha256);
            INSERT INTO lab.trajectory_records(
                experiment_id, trajectory_json, trajectory_sha256, trajectory_blob_sha256,
                messages_blob_sha256
            ) VALUES (p_experiment_id, p_trajectory_json, p_trajectory_sha256,
                      p_trajectory_blob_sha256, p_messages_blob_sha256);
            RETURN jsonb_build_object('status','committed','experiment_id',p_experiment_id);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.experiment_record_receipt(p_experiment_id text)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE exp lab.experiments%ROWTYPE; rec lab.experiment_records%ROWTYPE;
            traj lab.trajectory_records%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'experiment receipt requires the Director role';
            END IF;
            SELECT * INTO exp FROM lab.experiments WHERE experiment_id = p_experiment_id;
            SELECT * INTO rec FROM lab.experiment_records WHERE experiment_id = p_experiment_id;
            SELECT * INTO traj FROM lab.trajectory_records WHERE experiment_id = p_experiment_id;
            IF exp.experiment_id IS NULL OR rec.experiment_id IS NULL OR
               traj.experiment_id IS NULL OR exp.status NOT IN
                    ('scored','crashed','abandoned','rejected') THEN
                RAISE EXCEPTION 'experiment record pair is incomplete';
            END IF;
            RETURN jsonb_build_object(
                'experiment_id', exp.experiment_id, 'run_id', exp.run_id,
                'status', exp.status, 'experiment_sha256', rec.experiment_sha256,
                'experiment_blob_sha256', rec.experiment_blob_sha256,
                'trajectory_sha256', traj.trajectory_sha256,
                'trajectory_blob_sha256', traj.trajectory_blob_sha256,
                'messages_blob_sha256', traj.messages_blob_sha256
            );
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE VIEW lab.dev_task_results WITH (security_barrier=true) AS
        SELECT s.run_id, s.experiment_id, s.evaluation_kind, s.task_id, s.seed,
               s.score->>'candidate_sha256' AS candidate_sha256,
               s.score->>'dataset_id' AS dataset_id,
               s.score->>'split_id' AS split_id,
               s.score->>'session_id' AS session_id,
               s.score->>'vus_pr' AS vus_pr,
               s.score->>'vus_roc' AS vus_roc,
               s.score->>'sample_count' AS sample_count,
               s.score->>'profile_sha256' AS profile_sha256,
               s.score->>'harness_sha256' AS harness_sha256,
               s.score->>'candidate_output_sha256' AS candidate_output_sha256
          FROM scorer.task_scores s
          JOIN scorer.run_tasks t USING (run_id, experiment_id, evaluation_kind, task_id, seed)
          JOIN scorer.dataset_profiles p
            ON (p.dataset_id, p.split_id, p.session_id) =
               (t.dataset_id, t.split_id, t.session_id)
         WHERE p.visibility = 'dev'
           AND p.profile_sha256 = s.score->>'profile_sha256'
           AND t.candidate_sha256 = s.score->>'candidate_sha256'
           AND t.evaluation_kind IN ('baseline','primary','confirmation')
        """
    )
    op.execute(
        "REVOKE ALL ON lab.experiments, lab.experiment_records, lab.trajectory_records FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON lab.experiments, lab.experiment_records, lab.trajectory_records "
        "FROM swapp_lab_director, swapp_lab_planner, swapp_lab_scorer"
    )
    op.execute("GRANT SELECT ON lab.experiments TO swapp_lab_director")
    op.execute(f"GRANT EXECUTE ON FUNCTION {REGISTER_SIGNATURE} TO swapp_lab_director")
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.transition_experiment(text,text) TO swapp_lab_director"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {COMMIT_SIGNATURE} TO swapp_lab_director")
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.experiment_record_receipt(text) TO swapp_lab_director"
    )
    op.execute("GRANT SELECT ON lab.dev_task_results TO swapp_lab_director")
    op.execute("GRANT DELETE ON lab.experiments TO swapp_lab_migrator")
    op.execute(
        "GRANT DELETE ON lab.experiment_records, lab.trajectory_records TO swapp_lab_migrator"
    )
    op.execute("GRANT USAGE ON SCHEMA lab TO swapp_lab_planner")
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.lock_run_plan(uuid) TO swapp_lab_director, swapp_lab_planner"
    )
    for function in (
        "scorer.guard_dataset_visibility_immutable()",
        "lab.lock_run_plan(uuid)",
        "lab.guard_experiment_lifecycle()",
        "scorer.guard_experiment_task_identity()",
        "lab.guard_experiment_record_insert()",
        "lab.guard_trajectory_record_insert()",
        "lab.reject_immutable_document_mutation()",
        REGISTER_SIGNATURE,
        "lab.transition_experiment(text,text)",
        COMMIT_SIGNATURE,
        "lab.experiment_record_receipt(text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")


def downgrade() -> None:
    """Remove Director experiment records and dev-only view."""
    for table in ("experiment_records", "trajectory_records"):
        op.execute(f"DROP TRIGGER trg_{table}_immutable ON lab.{table}")
    op.execute("DROP TRIGGER trg_trajectory_record_insert ON lab.trajectory_records")
    op.execute("DROP TRIGGER trg_experiment_record_insert ON lab.experiment_records")
    op.execute("DROP TRIGGER trg_experiment_task_identity ON scorer.run_tasks")
    op.execute("DROP TRIGGER trg_experiment_lifecycle ON lab.experiments")
    op.execute("DROP TRIGGER trg_dataset_visibility_immutable ON scorer.dataset_profiles")
    op.execute("DROP VIEW lab.dev_task_results")
    op.execute("DROP FUNCTION lab.reject_immutable_document_mutation()")
    op.execute("DROP FUNCTION lab.guard_trajectory_record_insert()")
    op.execute("DROP FUNCTION lab.guard_experiment_record_insert()")
    op.execute("DROP FUNCTION scorer.guard_experiment_task_identity()")
    op.execute("DROP FUNCTION lab.guard_experiment_lifecycle()")
    op.execute("DROP FUNCTION lab.lock_run_plan(uuid)")
    op.execute(f"DROP FUNCTION {COMMIT_SIGNATURE}")
    op.execute("DROP FUNCTION lab.experiment_record_receipt(text)")
    op.execute("DROP FUNCTION lab.transition_experiment(text,text)")
    op.execute(f"DROP FUNCTION {REGISTER_SIGNATURE}")
    op.execute("DROP FUNCTION scorer.guard_dataset_visibility_immutable()")
    op.drop_table("trajectory_records", schema="lab")
    op.drop_table("experiment_records", schema="lab")
    op.drop_table("experiments", schema="lab")
    op.drop_constraint(
        "ck_dataset_profile_visibility", "dataset_profiles", schema="scorer", type_="check"
    )
    op.drop_column("dataset_profiles", "visibility", schema="scorer")
