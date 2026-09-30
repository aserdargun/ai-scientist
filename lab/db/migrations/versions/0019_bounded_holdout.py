"""Add private, quota-bound holdout registrations and one-bit receipts."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0019_bounded_holdout"
down_revision = "0018_public_task_semantics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep holdout task bindings and metrics Scorer-only; expose a bounded bit."""
    op.create_table(
        "holdout_suite_versions",
        sa.Column("suite_id", sa.String(128), primary_key=True),
        sa.Column("suite_version", sa.Integer, primary_key=True),
        # This is the separate hidden-set bundle digest. The development input is
        # pinned independently so the API request can be checked without exposing
        # the hidden-set manifest to Director.
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("development_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("task_count", sa.Integer, nullable=False),
        sa.Column("epsilon", sa.Float, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("suite_version > 0", name="ck_holdout_version_positive"),
        sa.CheckConstraint("task_count > 0 and task_count <= 256", name="ck_holdout_task_count"),
        sa.CheckConstraint("epsilon >= 0 and epsilon < 1", name="ck_holdout_epsilon"),
        sa.CheckConstraint("manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_holdout_manifest_sha"),
        sa.CheckConstraint(
            "development_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_holdout_development_manifest_sha",
        ),
        schema="scorer",
    )
    op.create_table(
        "holdout_suite_tasks",
        sa.Column("suite_id", sa.String(128), primary_key=True),
        sa.Column("suite_version", sa.Integer, primary_key=True),
        sa.Column("task_key", sa.String(64), primary_key=True),
        sa.Column("dataset_id", sa.String(128), nullable=False),
        sa.Column("split_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(256), nullable=False),
        sa.Column("profile_sha256", sa.String(64), nullable=False),
        sa.Column("family", sa.String(8), nullable=False),
        sa.Column("task_weight", sa.Float, nullable=False),
        sa.Column("base_score", sa.Float, nullable=False),
        sa.Column("reference_score", sa.Float, nullable=False),
        sa.Column("sliding_window", sa.Integer, nullable=False),
        sa.Column("sampling_s", sa.Integer),
        sa.Column("train_sha256", sa.String(64), nullable=False),
        sa.Column("evaluation_sha256", sa.String(64), nullable=False),
        sa.Column("semantics_sha256", sa.String(64), nullable=False),
        sa.Column("context_json", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.ForeignKeyConstraint(
            ("suite_id", "suite_version"),
            (
                "scorer.holdout_suite_versions.suite_id",
                "scorer.holdout_suite_versions.suite_version",
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("dataset_id", "split_id", "session_id"),
            (
                "scorer.dataset_profiles.dataset_id",
                "scorer.dataset_profiles.split_id",
                "scorer.dataset_profiles.session_id",
            ),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("task_key ~ '^[0-9a-f]{32}$'", name="ck_holdout_task_key"),
        sa.CheckConstraint("profile_sha256 ~ '^[0-9a-f]{64}$'", name="ck_holdout_profile_sha"),
        sa.CheckConstraint("family in ('EVT','PDM','NRM')", name="ck_holdout_family"),
        sa.CheckConstraint(
            "task_weight > 0 and task_weight < 'Infinity'::float8",
            name="ck_holdout_task_weight",
        ),
        sa.CheckConstraint(
            "base_score >= 0 and base_score <= reference_score and reference_score <= 1",
            name="ck_holdout_normalization",
        ),
        sa.CheckConstraint("sliding_window > 0", name="ck_holdout_window"),
        sa.CheckConstraint(
            "(family='EVT' and (sampling_s is null or sampling_s > 0)) or "
            "(family in ('PDM','NRM') and sampling_s is not null and sampling_s > 0)",
            name="ck_holdout_sampling",
        ),
        sa.CheckConstraint("train_sha256 ~ '^[0-9a-f]{64}$'", name="ck_holdout_train_sha"),
        sa.CheckConstraint("evaluation_sha256 ~ '^[0-9a-f]{64}$'", name="ck_holdout_eval_sha"),
        sa.CheckConstraint("semantics_sha256 ~ '^[0-9a-f]{64}$'", name="ck_holdout_semantics_sha"),
        schema="scorer",
    )
    op.create_table(
        "holdout_suite_quotas",
        sa.Column("suite_id", sa.String(128), primary_key=True),
        sa.Column("suite_version", sa.Integer, primary_key=True),
        sa.Column("used", sa.Integer, nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ("suite_id", "suite_version"),
            (
                "scorer.holdout_suite_versions.suite_id",
                "scorer.holdout_suite_versions.suite_version",
            ),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("used >= 0 and used <= 100", name="ck_holdout_suite_quota"),
        schema="lab",
    )
    op.create_table(
        "holdout_run_end_intents",
        sa.Column("run_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("terminal_status", sa.String(32), nullable=False),
        sa.Column("completed_proposals", sa.Integer, nullable=False),
        sa.Column("proposal_limit", sa.Integer, nullable=False),
        sa.Column("candidate_experiment_id", sa.String(128), nullable=False),
        sa.Column("candidate_sha256", sa.String(64), nullable=False),
        sa.Column("state_checkpoint_key", sa.String(160), nullable=False),
        sa.Column("state_checkpoint_sha256", sa.String(64), nullable=False),
        sa.Column("state_checkpoint_sequence", sa.Integer, nullable=False),
        sa.Column("intent_checkpoint_sha256", sa.String(64), nullable=False),
        sa.Column("intent_checkpoint_sequence", sa.Integer, nullable=False),
        sa.Column("wall_seconds", sa.Float, nullable=False),
        sa.Column("elapsed_wall_seconds", sa.Float, nullable=False),
        sa.Column("reserved_wall_seconds", sa.Float, nullable=False),
        sa.Column("model_tokens", sa.Integer, nullable=False),
        sa.Column("reserved_model_tokens", sa.Integer, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["run_id"], ["lab.runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["candidate_experiment_id"], ["lab.experiments.experiment_id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "terminal_status in ('proposal_limit_reached','budget_exhausted')",
            name="ck_holdout_run_end_status",
        ),
        sa.CheckConstraint("completed_proposals >= 0", name="ck_holdout_run_end_completed"),
        sa.CheckConstraint("proposal_limit > 0", name="ck_holdout_run_end_limit"),
        sa.CheckConstraint(
            "completed_proposals <= proposal_limit", name="ck_holdout_run_end_count"
        ),
        sa.CheckConstraint(
            "candidate_sha256 ~ '^[0-9a-f]{64}$'", name="ck_holdout_run_end_candidate_sha"
        ),
        sa.CheckConstraint(
            "length(state_checkpoint_key) > 0",
            name="ck_holdout_run_end_state_key",
        ),
        sa.CheckConstraint(
            "state_checkpoint_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_holdout_run_end_state_sha",
        ),
        sa.CheckConstraint(
            "intent_checkpoint_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_holdout_run_end_intent_sha",
        ),
        sa.CheckConstraint(
            "state_checkpoint_sequence >= 0 and intent_checkpoint_sequence > "
            "state_checkpoint_sequence",
            name="ck_holdout_run_end_sequences",
        ),
        sa.CheckConstraint(
            "wall_seconds >= 0 and elapsed_wall_seconds >= 0 and reserved_wall_seconds >= 0",
            name="ck_holdout_run_end_wall",
        ),
        sa.CheckConstraint(
            "model_tokens >= 0 and reserved_model_tokens >= 0",
            name="ck_holdout_run_end_tokens",
        ),
        schema="lab",
    )
    op.create_table(
        "holdout_run_quotas",
        sa.Column("run_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("suite_id", sa.String(128), nullable=False),
        sa.Column("suite_version", sa.Integer, nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("used", sa.Integer, nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["run_id"], ["lab.runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ("suite_id", "suite_version"),
            (
                "scorer.holdout_suite_versions.suite_id",
                "scorer.holdout_suite_versions.suite_version",
            ),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("used >= 0 and used <= 20", name="ck_holdout_run_quota"),
        sa.CheckConstraint("manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_holdout_run_manifest"),
        schema="lab",
    )
    op.create_table(
        "holdout_approvals",
        sa.Column("run_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("candidate_sha256", sa.String(64), nullable=False),
        sa.Column("reservation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["run_id"], ["lab.runs.run_id"], ondelete="CASCADE"),
        sa.CheckConstraint("candidate_sha256 ~ '^[0-9a-f]{64}$'", name="ck_holdout_approval_sha"),
        schema="lab",
    )
    op.create_table(
        "holdout_reservations",
        sa.Column("reservation_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("suite_id", sa.String(128), nullable=False),
        sa.Column("suite_version", sa.Integer, nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("candidate_experiment_id", sa.String(128), nullable=False),
        sa.Column("candidate_sha256", sa.String(64), nullable=False),
        sa.Column("candidate_blob_sha256", sa.String(64), nullable=False),
        sa.Column("reference_sha256", sa.String(64), nullable=False),
        sa.Column("reference_blob_sha256", sa.String(64), nullable=False),
        sa.Column("trigger_kind", sa.String(16), nullable=False),
        sa.Column("trigger_index", sa.Integer, nullable=False),
        sa.Column("epsilon", sa.Float, nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="reserved"),
        sa.Column("worker_pid", sa.Integer),
        sa.Column("worker_start_ticks", sa.BigInteger),
        sa.Column("worker_boot_id", sa.String(36)),
        sa.Column("worker_unit", sa.String(255)),
        sa.Column("worker_invocation_id", sa.String(32)),
        sa.Column("worker_cgroup", sa.Text),
        sa.Column("result_bit", sa.Boolean),
        sa.Column("result_sha256", sa.String(64)),
        sa.Column("error_code", sa.String(48)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["run_id"], ["lab.runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ("suite_id", "suite_version"),
            (
                "scorer.holdout_suite_versions.suite_id",
                "scorer.holdout_suite_versions.suite_version",
            ),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("run_id", "reservation_id", name="uq_holdout_run_reservation"),
        sa.UniqueConstraint("run_id", "request_key", name="uq_holdout_run_request"),
        sa.UniqueConstraint(
            "run_id", "trigger_kind", "trigger_index", name="uq_holdout_run_trigger"
        ),
        sa.CheckConstraint("length(request_key) between 1 and 128", name="ck_holdout_request_key"),
        sa.CheckConstraint(
            "manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_holdout_reservation_manifest"
        ),
        sa.CheckConstraint(
            "candidate_blob_sha256 ~ '^[0-9a-f]{64}$'", name="ck_holdout_candidate_blob_sha"
        ),
        sa.CheckConstraint("reference_sha256 ~ '^[0-9a-f]{64}$'", name="ck_holdout_reference_sha"),
        sa.CheckConstraint(
            "reference_blob_sha256 ~ '^[0-9a-f]{64}$'", name="ck_holdout_reference_blob_sha"
        ),
        sa.CheckConstraint(
            "trigger_kind in ('keep_interval','run_end')", name="ck_holdout_trigger"
        ),
        sa.CheckConstraint("trigger_index > 0", name="ck_holdout_trigger_index"),
        sa.CheckConstraint("epsilon >= 0 and epsilon < 1", name="ck_holdout_reservation_epsilon"),
        sa.CheckConstraint(
            "state in ('reserved','running','passed','reverted','failed','exhausted')",
            name="ck_holdout_reservation_state",
        ),
        sa.CheckConstraint(
            "(worker_pid is null and worker_start_ticks is null and worker_boot_id is null "
            "and worker_unit is null and worker_invocation_id is null and worker_cgroup is null) "
            "or (worker_pid is not null and worker_start_ticks is not null and "
            "worker_boot_id is not null "
            "and worker_unit is not null and worker_invocation_id is not null and "
            "worker_cgroup is not null "
            "and worker_pid > 1 and worker_start_ticks > 0 and length(worker_boot_id)=36 "
            "and length(worker_unit)>0 and length(worker_invocation_id)=32 and "
            "length(worker_cgroup)>0)",
            name="ck_holdout_worker_identity",
        ),
        sa.CheckConstraint(
            "(state in ('passed','reverted') and result_bit is not null and "
            "result_sha256 is not null and error_code is null) "
            "or (state in ('reserved','running','exhausted') and result_bit is "
            "null and result_sha256 is null and error_code is null) "
            "or (state='failed' and result_bit is null and result_sha256 is null "
            "and error_code is not null)",
            name="ck_holdout_result_shape",
        ),
        schema="lab",
    )
    op.create_table(
        "holdout_results",
        sa.Column("reservation_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("result_json", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.Column("delta", sa.Float, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["lab.holdout_reservations.reservation_id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint("result_sha256 ~ '^[0-9a-f]{64}$'", name="ck_holdout_result_sha"),
        schema="scorer",
    )

    op.execute(
        "REVOKE ALL ON scorer.holdout_suite_versions, scorer.holdout_suite_tasks, "
        "scorer.holdout_results, lab.holdout_suite_quotas, lab.holdout_run_quotas, "
        "lab.holdout_approvals, lab.holdout_reservations FROM PUBLIC, "
        "swapp_lab_director, swapp_lab_planner"
    )
    op.execute(
        "REVOKE ALL ON lab.holdout_run_end_intents FROM PUBLIC, "
        "swapp_lab_scorer, swapp_lab_planner"
    )
    op.execute("GRANT SELECT ON lab.holdout_run_end_intents TO swapp_lab_director")
    op.execute(
        "GRANT SELECT, INSERT ON scorer.holdout_suite_versions, "
        "scorer.holdout_suite_tasks TO swapp_lab_scorer"
    )
    op.execute("GRANT SELECT ON scorer.holdout_results TO swapp_lab_scorer")
    op.create_foreign_key(
        "fk_holdout_approval_reservation",
        "holdout_approvals",
        "holdout_reservations",
        ["run_id", "reservation_id"],
        ["run_id", "reservation_id"],
        source_schema="lab",
        referent_schema="lab",
        ondelete="RESTRICT",
    )

    op.execute(
        """
        CREATE FUNCTION lab.guard_holdout_profile_binding()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,scorer AS $$
        DECLARE profile scorer.dataset_profiles%ROWTYPE;
        DECLARE semantics scorer.dataset_task_semantics%ROWTYPE;
        DECLARE suite_row scorer.holdout_suite_versions%ROWTYPE; n integer;
        BEGIN
            IF session_user NOT IN ('swapp_lab_scorer','swapp_lab_migrator') THEN
                RAISE EXCEPTION 'holdout registration requires Scorer';
            END IF;
            IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'holdout registry is immutable'; END IF;
            SELECT * INTO profile FROM scorer.dataset_profiles
             WHERE dataset_id=NEW.dataset_id AND split_id=NEW.split_id AND
             session_id=NEW.session_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'holdout profile is unavailable';
            END IF;
            SELECT * INTO semantics FROM scorer.dataset_task_semantics
             WHERE dataset_id=NEW.dataset_id AND split_id=NEW.split_id AND
             session_id=NEW.session_id;
            IF profile.visibility <> 'holdout'
               OR profile.profile_sha256 <> NEW.profile_sha256
               OR profile.task_family <> NEW.family
               OR profile.sample_count < NEW.sliding_window THEN
                RAISE EXCEPTION 'holdout task does not match an immutable holdout profile';
            END IF;
            IF semantics.dataset_id IS NULL OR semantics.task_family <> NEW.family
               OR semantics.semantics_sha256 <> NEW.semantics_sha256
               OR semantics.sampling_s IS DISTINCT FROM NEW.sampling_s THEN
                RAISE EXCEPTION 'holdout task semantics differ from Scorer registration';
            END IF;
            SELECT * INTO suite_row FROM scorer.holdout_suite_versions
             WHERE suite_id=NEW.suite_id AND suite_version=NEW.suite_version FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout suite version is not registered'; END IF;
            SELECT count(*) INTO n FROM scorer.holdout_suite_tasks
             WHERE suite_id=NEW.suite_id AND suite_version=NEW.suite_version;
            IF n >= suite_row.task_count THEN
                RAISE EXCEPTION 'holdout suite task count exceeds its manifest';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER holdout_task_profile_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON scorer.holdout_suite_tasks FOR EACH ROW EXECUTE FUNCTION "
        "lab.guard_holdout_profile_binding()"
    )
    op.execute(
        """
        CREATE FUNCTION lab.guard_holdout_reservation()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab,scorer AS $$
        BEGIN
            IF TG_OP='INSERT' THEN
                IF session_user <> 'swapp_lab_director' OR NEW.state NOT IN
                ('reserved','exhausted') THEN
                    RAISE EXCEPTION 'holdout reservation requires Director admission';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP='DELETE' THEN RAISE EXCEPTION 'holdout reservations are immutable'; END IF;
            IF ROW(NEW.run_id,NEW.request_key,NEW.suite_id,NEW.suite_version,NEW.manifest_sha256,
                   NEW.candidate_experiment_id,NEW.candidate_sha256,NEW.candidate_blob_sha256,
                   NEW.reference_sha256,NEW.reference_blob_sha256,
                   NEW.trigger_kind,NEW.trigger_index,NEW.epsilon)
               IS DISTINCT FROM
               ROW(OLD.run_id,OLD.request_key,OLD.suite_id,OLD.suite_version,OLD.manifest_sha256,
                   OLD.candidate_experiment_id,OLD.candidate_sha256,OLD.candidate_blob_sha256,
                   OLD.reference_sha256,OLD.reference_blob_sha256,
                   OLD.trigger_kind,OLD.trigger_index,OLD.epsilon) THEN
                RAISE EXCEPTION 'holdout reservation identity is immutable';
            END IF;
            IF session_user='swapp_lab_scorer' THEN
                IF OLD.state NOT IN ('reserved','running') OR NEW.state NOT IN
                ('running','passed','reverted','failed') THEN
                    RAISE EXCEPTION 'invalid Scorer holdout state transition';
                END IF;
                IF OLD.worker_pid IS NOT NULL AND ROW(NEW.worker_pid,NEW.worker_start_ticks,
                   NEW.worker_boot_id,NEW.worker_unit,NEW.worker_invocation_id,NEW.worker_cgroup)
                   IS DISTINCT FROM ROW(OLD.worker_pid,OLD.worker_start_ticks,OLD.worker_boot_id,
                   OLD.worker_unit,OLD.worker_invocation_id,OLD.worker_cgroup) THEN
                    RAISE EXCEPTION 'holdout worker generation is immutable';
                END IF;
                IF OLD.state='reserved' AND NEW.state='running' AND (
                    NEW.worker_pid IS NULL OR NEW.worker_start_ticks IS NULL OR
                    NEW.worker_boot_id IS NULL
                    OR NEW.worker_unit IS NULL OR NEW.worker_invocation_id IS NULL OR
                    NEW.worker_cgroup IS NULL
                    OR NEW.worker_unit <> 'swapp-ai-scientist-scorer-' ||
                    replace(NEW.reservation_id::text,'-','') || '.service'
                    OR NEW.worker_cgroup NOT LIKE '%/' || NEW.worker_unit
                ) THEN
                    RAISE EXCEPTION 'running holdout reservation requires its exact worker
                    generation';
                END IF;
            ELSIF session_user='swapp_lab_director' THEN
                RAISE EXCEPTION 'Director cannot update holdout results';
            ELSE
                RAISE EXCEPTION 'holdout update requires Scorer';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER holdout_reservation_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON lab.holdout_reservations FOR EACH ROW EXECUTE FUNCTION lab.guard_holdout_reservation()"
    )

    op.execute(
        """
        CREATE FUNCTION lab.guard_holdout_run_end_no_more_experiments()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM lab.holdout_run_end_intents WHERE run_id=NEW.run_id) THEN
                RAISE EXCEPTION 'run has a durable terminal holdout intent';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER holdout_run_end_blocks_experiments BEFORE INSERT ON lab.experiments "
        "FOR EACH ROW EXECUTE FUNCTION lab.guard_holdout_run_end_no_more_experiments()"
    )
    op.execute(
        """
        CREATE FUNCTION lab.register_holdout_run_end_intent(p_run_id uuid, p_intent jsonb)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab,scorer AS $$
        DECLARE r lab.runs%ROWTYPE; cal lab.baseline_calibrations%ROWTYPE;
        DECLARE version_row scorer.holdout_suite_versions%ROWTYPE;
        DECLARE state_receipt jsonb; intent_receipt jsonb; state_marker jsonb;
        DECLARE existing lab.holdout_run_end_intents%ROWTYPE;
        DECLARE state_key text; state_sha text; state_seq integer;
        DECLARE marker_key text; marker_sha text; marker_application_sha text;
        DECLARE latest_seq integer; marker_keep_count text;
        DECLARE intent_sha text; intent_seq integer; candidate_id text; candidate_sha text;
        DECLARE terminal_status text; completed integer; proposal_limit integer;
        DECLARE wall_seconds double precision; elapsed_wall_seconds double precision;
        DECLARE reserved_wall_seconds double precision; model_tokens integer;
        DECLARE reserved_model_tokens integer; request_wall integer; request_tokens integer;
        DECLARE ordinal integer; experiment_rows integer; abandoned_rows integer;
        DECLARE claimed_abandonments integer; valid_abandonments integer := 0;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'Director role required';
            END IF;
            IF jsonb_typeof(p_intent) <> 'object'
               OR p_intent->>'schema' IS DISTINCT FROM 'director-holdout-run-end-intent.v1' THEN
                RAISE EXCEPTION 'holdout run-end intent is malformed';
            END IF;
            state_key := p_intent->>'state_checkpoint_key';
            state_sha := p_intent->>'state_checkpoint_sha256';
            state_seq := (p_intent->>'state_checkpoint_sequence')::integer;
            marker_key := p_intent->>'state_application_key';
            marker_sha := p_intent->>'state_application_sha256';
            marker_application_sha := p_intent->>'state_application_receipt_sha256';
            candidate_id := p_intent->>'candidate_experiment_id';
            candidate_sha := p_intent->>'candidate_sha256';
            terminal_status := p_intent->>'terminal_status';
            completed := (p_intent->>'completed_proposals')::integer;
            proposal_limit := (p_intent->>'proposal_limit')::integer;
            wall_seconds := (p_intent->'budget'->>'wall_seconds')::double precision;
            elapsed_wall_seconds := (p_intent->'budget'->>'elapsed_wall_seconds')::double precision;
            reserved_wall_seconds :=
                (p_intent->'budget'->>'reserved_wall_seconds')::double precision;
            model_tokens := (p_intent->'budget'->>'model_tokens')::integer;
            reserved_model_tokens :=
                (p_intent->'budget'->>'reserved_model_tokens')::integer;
            IF jsonb_typeof(p_intent->'abandoned_checkpoints') IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION 'run-end abandoned checkpoint list is malformed';
            END IF;
            claimed_abandonments := jsonb_array_length(p_intent->'abandoned_checkpoints');
            IF state_key IS NULL
               OR state_key !~ ('^director-state:' || completed::text ||
                    '([:]holdout:[1-9][0-9]*([:]keep_interval)?'
                    '([:]run_end_budget_reserved)?|[:]run_end_budget_reserved)?$')
               OR state_sha IS NULL OR state_sha !~ '^[0-9a-f]{64}$'
               OR candidate_id IS NULL OR length(candidate_id) NOT BETWEEN 1 AND 128
               OR candidate_sha IS NULL OR candidate_sha !~ '^[0-9a-f]{64}$'
               OR terminal_status IS NULL OR completed IS NULL OR proposal_limit IS NULL
               OR wall_seconds IS NULL OR elapsed_wall_seconds IS NULL
               OR reserved_wall_seconds IS NULL OR model_tokens IS NULL
               OR reserved_model_tokens IS NULL
               OR wall_seconds::text IN ('NaN','Infinity','-Infinity')
               OR elapsed_wall_seconds::text IN ('NaN','Infinity','-Infinity')
               OR reserved_wall_seconds::text IN ('NaN','Infinity','-Infinity')
               OR state_seq < 0 OR completed < 0 OR proposal_limit < 1
               OR completed > proposal_limit OR wall_seconds < 0
               OR elapsed_wall_seconds < 0 OR reserved_wall_seconds < 0
               OR model_tokens < 0 OR reserved_model_tokens < 0
               OR terminal_status NOT IN ('proposal_limit_reached','budget_exhausted')
               OR ((marker_key IS NULL OR marker_sha IS NULL OR marker_application_sha IS NULL)
                   AND NOT (marker_key IS NULL AND marker_sha IS NULL
                       AND marker_application_sha IS NULL))
               OR (marker_sha IS NOT NULL AND marker_sha !~ '^[0-9a-f]{64}$')
               OR (marker_application_sha IS NOT NULL
                   AND marker_application_sha !~ '^[0-9a-f]{64}$') THEN
                RAISE EXCEPTION 'holdout run-end intent identity is invalid';
            END IF;
            SELECT * INTO r FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
            IF NOT FOUND OR r.state <> 'running' OR r.stop_requested THEN
                RAISE EXCEPTION 'run is no longer active';
            END IF;
            SELECT * INTO cal FROM lab.baseline_calibrations WHERE run_id=p_run_id;
            SELECT * INTO version_row FROM scorer.holdout_suite_versions
             WHERE suite_id=cal.suite_id AND suite_version=cal.suite_version;
            IF cal.run_id IS NULL OR version_row.suite_id IS NULL
               OR r.request_json->>'suite' IS DISTINCT FROM cal.suite_id
               OR version_row.development_manifest_sha256 IS DISTINCT FROM
                  r.request_json->>'suite_manifest_sha256' THEN
                RAISE EXCEPTION 'run-end suite identity is not registered';
            END IF;
            request_wall := (r.request_json->'budget'->>'wall_seconds')::integer;
            request_tokens := (r.request_json->'budget'->>'model_tokens')::integer;
            IF request_wall IS NULL OR request_tokens IS NULL
               OR request_wall < 1 OR request_tokens < 0
               OR proposal_limit IS DISTINCT FROM
               (r.request_json->>'proposal_limit')::integer
               OR proposal_limit IS DISTINCT FROM
               (r.request_json->'budget'->>'experiments')::integer THEN
                RAISE EXCEPTION 'run-end proposal limit differs from the immutable request';
            END IF;
            IF terminal_status='proposal_limit_reached' THEN
                IF completed <> proposal_limit THEN
                    RAISE EXCEPTION 'proposal-limit termination is not complete';
                END IF;
            ELSIF request_wall - greatest(wall_seconds,elapsed_wall_seconds)
                       - reserved_wall_seconds >= 1
               AND request_tokens - model_tokens - reserved_model_tokens >= 1 THEN
                RAISE EXCEPTION 'budget-exhausted status is not supported by durable usage';
            END IF;
            IF EXISTS (
                SELECT 1 FROM lab.experiments WHERE run_id=p_run_id AND kind='proposal'
                AND status NOT IN ('scored','crashed','abandoned','rejected')
            ) OR EXISTS (
                SELECT 1 FROM lab.experiments WHERE run_id=p_run_id AND kind='proposal'
                AND experiment_number > completed
            ) THEN
                RAISE EXCEPTION 'proposal ledger is not terminal at run end';
            END IF;
            IF completed > 0 THEN
                FOR ordinal IN 1..completed LOOP
                    SELECT count(*) INTO experiment_rows FROM lab.experiments
                     WHERE run_id=p_run_id AND kind='proposal' AND experiment_number=ordinal;
                    SELECT count(*) INTO abandoned_rows
                      FROM jsonb_array_elements(p_intent->'abandoned_checkpoints') AS claim(value)
                      JOIN lab.run_events e ON e.run_id=p_run_id
                       AND e.event_type='director.checkpoint'
                       AND e.event_json->>'key'=claim.value->>'key'
                     WHERE claim.value->>'ordinal'=ordinal::text
                       AND claim.value->>'key'= ('proposal-abandoned:' || ordinal::text)
                       AND claim.value->>'payload_sha256' ~ '^[0-9a-f]{64}$'
                       AND e.event_json->>'phase'='proposal_abandoned'
                       AND e.event_json->>'payload_sha256'=
                           claim.value->>'payload_sha256';
                    valid_abandonments := valid_abandonments + abandoned_rows;
                    IF experiment_rows + abandoned_rows <> 1 THEN
                        RAISE EXCEPTION 'proposal ordinal lacks one terminal receipt';
                    END IF;
                END LOOP;
            END IF;
            IF claimed_abandonments <> valid_abandonments THEN
                RAISE EXCEPTION 'run-end abandoned checkpoint claims are not exact';
            END IF;
            SELECT event_json INTO state_receipt FROM lab.run_events
             WHERE run_id=p_run_id AND event_type='director.checkpoint'
               AND event_json->>'key'=state_key;
            IF state_receipt IS NULL
               OR state_receipt->>'phase' IS DISTINCT FROM 'director_loop_state'
               OR (state_receipt->>'sequence')::integer IS DISTINCT FROM state_seq
               OR state_receipt->>'payload_sha256' IS DISTINCT FROM state_sha THEN
                RAISE EXCEPTION 'run-end state checkpoint receipt is unavailable';
            END IF;
            SELECT event_json INTO intent_receipt FROM lab.run_events
             WHERE run_id=p_run_id AND event_type='director.checkpoint'
               AND event_json->>'phase'='director_loop_state'
             ORDER BY (event_json->>'sequence')::integer DESC LIMIT 1;
            IF intent_receipt->>'key' IS DISTINCT FROM state_key
               OR (intent_receipt->>'sequence')::integer IS DISTINCT FROM state_seq
               OR intent_receipt->>'payload_sha256' IS DISTINCT FROM state_sha THEN
                RAISE EXCEPTION 'run-end state checkpoint is not the latest Director state';
            END IF;
            SELECT max((event_json->>'sequence')::integer) INTO latest_seq
             FROM lab.run_events WHERE run_id=p_run_id AND event_type='director.checkpoint'
               AND event_json->>'key' <> 'director-holdout-run-end-intent';
            IF latest_seq IS DISTINCT FROM state_seq THEN
                IF state_key ~ '[:]holdout[:]([1-9][0-9]*)[:]keep_interval$' THEN
                    marker_keep_count := substring(state_key from
                        '[:]holdout[:]([1-9][0-9]*)[:]keep_interval$');
                    IF marker_key IS DISTINCT FROM
                           'holdout-state-application' || chr(58) || 'keep_interval' ||
                           chr(58) || marker_keep_count
                       OR marker_sha IS NULL OR marker_application_sha IS NULL THEN
                        RAISE EXCEPTION 'run-end state lacks its exact holdout application marker';
                    END IF;
                    SELECT event_json INTO state_marker FROM lab.run_events
                     WHERE run_id=p_run_id AND event_type='director.checkpoint'
                       AND event_json->>'key'=marker_key;
                    IF state_marker IS NULL
                       OR state_marker->>'phase' IS DISTINCT FROM 'holdout_state_application'
                       OR state_marker->>'payload_sha256' IS DISTINCT FROM marker_sha
                       OR (state_marker->>'sequence')::integer IS DISTINCT FROM latest_seq THEN
                        RAISE EXCEPTION
                            'run-end application marker is not the exact latest checkpoint';
                    END IF;
                    PERFORM 1 FROM lab.run_events
                     WHERE run_id=p_run_id AND event_type='director.checkpoint'
                       AND event_json->>'key' =
                           'holdout-application' || chr(58) || 'keep_interval' ||
                           chr(58) || marker_keep_count
                       AND event_json->>'phase'='holdout_application'
                       AND event_json->>'payload_sha256'=marker_application_sha;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'run-end application marker receipt is unavailable';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'run-end state checkpoint has an unrelated newer event';
                END IF;
            ELSIF marker_key IS NOT NULL THEN
                RAISE EXCEPTION 'run-end state marker is not the latest checkpoint';
            END IF;
            SELECT event_json INTO intent_receipt FROM lab.run_events
             WHERE run_id=p_run_id AND event_type='director.checkpoint'
               AND event_json->>'key'='director-holdout-run-end-intent';
            IF intent_receipt IS NULL
               OR intent_receipt->>'phase' IS DISTINCT FROM 'holdout_run_end_intent'
               OR (intent_receipt->>'sequence')::integer <= state_seq THEN
                RAISE EXCEPTION 'terminal holdout intent checkpoint is unavailable';
            END IF;
            intent_sha := intent_receipt->>'payload_sha256';
            intent_seq := (intent_receipt->>'sequence')::integer;
            IF intent_sha !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'terminal holdout intent checkpoint digest is invalid';
            END IF;
            PERFORM 1 FROM lab.experiments
             WHERE experiment_id=candidate_id AND run_id=p_run_id AND status='scored'
               AND candidate_sha256=candidate_sha
               AND ((kind='baseline' AND baseline_name='robust_z') OR
                    (kind='proposal' AND EXISTS (
                       SELECT 1 FROM lab.experiment_records er
                        WHERE er.experiment_id=candidate_id AND
                        er.experiment_json->'decision'->>'verdict' IN ('KEEP','KEEP_SIMPLER')
                    )));
            IF NOT FOUND THEN RAISE EXCEPTION 'run-end champion is not an eligible candidate';
            END IF;
            SELECT * INTO existing FROM lab.holdout_run_end_intents WHERE run_id=p_run_id;
            IF FOUND THEN
                IF ROW(existing.terminal_status,existing.completed_proposals,
                       existing.proposal_limit,
                       existing.candidate_experiment_id,existing.candidate_sha256,
                       existing.state_checkpoint_key,existing.state_checkpoint_sha256,
                       existing.state_checkpoint_sequence,existing.intent_checkpoint_sha256,
                       existing.intent_checkpoint_sequence,existing.wall_seconds,
                       existing.elapsed_wall_seconds,existing.reserved_wall_seconds,
                       existing.model_tokens,existing.reserved_model_tokens)
                   IS DISTINCT FROM
                   ROW(terminal_status,completed,proposal_limit,candidate_id,candidate_sha,
                       state_key,state_sha,state_seq,intent_sha,intent_seq,wall_seconds,
                       elapsed_wall_seconds,reserved_wall_seconds,model_tokens,
                       reserved_model_tokens) THEN
                    RAISE EXCEPTION 'run-end intent conflicts with its durable receipt';
                END IF;
                RETURN jsonb_build_object('status','already_registered',
                    'run_id',p_run_id,'checkpoint_sha256',intent_sha);
            END IF;
            INSERT INTO lab.holdout_run_end_intents(
                run_id,terminal_status,completed_proposals,proposal_limit,
                candidate_experiment_id,candidate_sha256,state_checkpoint_key,
                state_checkpoint_sha256,state_checkpoint_sequence,intent_checkpoint_sha256,
                intent_checkpoint_sequence,wall_seconds,elapsed_wall_seconds,
                reserved_wall_seconds,model_tokens,reserved_model_tokens
            ) VALUES (
                p_run_id,terminal_status,completed,proposal_limit,candidate_id,candidate_sha,
                state_key,state_sha,state_seq,intent_sha,intent_seq,wall_seconds,
                elapsed_wall_seconds,reserved_wall_seconds,model_tokens,reserved_model_tokens
            );
            RETURN jsonb_build_object('status','registered','run_id',p_run_id,
                'checkpoint_sha256',intent_sha);
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION lab.register_holdout_run_end_intent(uuid,jsonb) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.register_holdout_run_end_intent(uuid,jsonb) "
        "TO swapp_lab_director"
    )
    op.execute("REVOKE ALL ON FUNCTION lab.guard_holdout_profile_binding() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION lab.guard_holdout_reservation() FROM PUBLIC")
    op.execute(
        "REVOKE ALL ON FUNCTION lab.guard_holdout_run_end_no_more_experiments() FROM PUBLIC"
    )

    op.execute(
        """
        CREATE FUNCTION lab.holdout_suite_is_registered(p_run_id uuid)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab,scorer AS $$
        DECLARE r lab.runs%ROWTYPE; cal lab.baseline_calibrations%ROWTYPE;
        DECLARE version_row scorer.holdout_suite_versions%ROWTYPE; task_rows integer;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'Director role required';
            END IF;
            SELECT * INTO r FROM lab.runs WHERE run_id=p_run_id;
            SELECT * INTO cal FROM lab.baseline_calibrations WHERE run_id=p_run_id;
            IF r.run_id IS NULL OR cal.run_id IS NULL
               OR r.request_json->>'suite' IS DISTINCT FROM cal.suite_id
               OR r.request_json->>'suite_manifest_sha256' IS NULL THEN
                RETURN false;
            END IF;
            SELECT * INTO version_row FROM scorer.holdout_suite_versions
             WHERE suite_id=cal.suite_id AND suite_version=cal.suite_version;
            IF version_row.suite_id IS NULL
               OR version_row.development_manifest_sha256 IS DISTINCT FROM
                  r.request_json->>'suite_manifest_sha256'
               OR version_row.holdout_manifest_sha256 !~ '^[0-9a-f]{64}$'
               OR version_row.task_count < 1 THEN
                RETURN false;
            END IF;
            SELECT count(*) INTO task_rows FROM scorer.holdout_suite_tasks
             WHERE suite_id=cal.suite_id AND suite_version=cal.suite_version;
            RETURN task_rows=version_row.task_count;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION lab.holdout_suite_is_registered(uuid) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.holdout_suite_is_registered(uuid) TO swapp_lab_director"
    )

    op.execute(
        """
        CREATE FUNCTION lab.read_holdout_request(
            p_run_id uuid, p_request_key text, p_candidate_experiment_id text,
            p_trigger_kind text, p_trigger_index integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab,scorer AS $$
        DECLARE existing lab.holdout_reservations%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN
                RAISE EXCEPTION 'Director role required';
            END IF;
            IF p_request_key IS NULL OR length(p_request_key) NOT BETWEEN 1 AND 128
               OR p_candidate_experiment_id IS NULL
               OR length(p_candidate_experiment_id) NOT BETWEEN 1 AND 128
               OR p_trigger_kind NOT IN ('keep_interval','run_end')
               OR p_trigger_index < 1 THEN
                RAISE EXCEPTION 'holdout request identity is invalid';
            END IF;
            SELECT * INTO existing FROM lab.holdout_reservations
             WHERE run_id=p_run_id AND request_key=p_request_key;
            IF NOT FOUND THEN RETURN NULL; END IF;
            IF existing.candidate_experiment_id IS DISTINCT FROM p_candidate_experiment_id
               OR existing.trigger_kind IS DISTINCT FROM p_trigger_kind
               OR existing.trigger_index IS DISTINCT FROM p_trigger_index THEN
                RAISE EXCEPTION 'holdout request key is bound to another candidate or trigger';
            END IF;
            RETURN jsonb_build_object(
                'run_id',existing.run_id,
                'reservation_id',existing.reservation_id,
                'candidate_experiment_id',existing.candidate_experiment_id,
                'trigger_kind',existing.trigger_kind,
                'trigger_index',existing.trigger_index,
                'state',existing.state,
                'bit',CASE WHEN existing.state IN ('passed','reverted')
                    THEN existing.result_bit ELSE NULL END
            );
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION lab.read_holdout_request(uuid,text,text,text,integer) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.read_holdout_request(uuid,text,text,text,integer) "
        "TO swapp_lab_director"
    )

    op.execute(
        """
        CREATE FUNCTION lab.reserve_holdout_check(
            p_run_id uuid, p_reservation_id uuid, p_request_key text,
            p_candidate_experiment_id text, p_trigger_kind text, p_trigger_index integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab,scorer AS $$
        DECLARE r lab.runs%ROWTYPE; cal lab.baseline_calibrations%ROWTYPE;
        DECLARE version_row scorer.holdout_suite_versions%ROWTYPE;
        DECLARE exp lab.experiments%ROWTYPE; existing lab.holdout_reservations%ROWTYPE;
        DECLARE approval lab.holdout_approvals%ROWTYPE;
        DECLARE end_intent lab.holdout_run_end_intents%ROWTYPE;
        DECLARE suite_used integer; run_used integer; ref_sha text; ref_blob text;
        DECLARE registered_count integer; keep_count integer;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN RAISE EXCEPTION 'Director role
            required'; END IF;
            IF p_request_key IS NULL OR length(p_request_key) NOT BETWEEN 1 AND 128
               OR p_trigger_kind NOT IN ('keep_interval','run_end') OR p_trigger_index < 1
               OR (p_trigger_kind='run_end' AND p_trigger_index <> 1) THEN
                RAISE EXCEPTION 'holdout request is invalid';
            END IF;
            SELECT * INTO r FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'run is unavailable'; END IF;
            SELECT * INTO cal FROM lab.baseline_calibrations WHERE run_id=p_run_id;
            IF NOT FOUND OR (r.request_json->>'suite') IS DISTINCT FROM cal.suite_id
               OR r.request_json->>'suite_manifest_sha256' IS NULL
               OR r.request_json->>'suite_manifest_sha256' !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'run has no immutable suite identity';
            END IF;
            SELECT * INTO version_row FROM scorer.holdout_suite_versions
             WHERE suite_id=cal.suite_id AND suite_version=cal.suite_version;
            IF NOT FOUND OR version_row.development_manifest_sha256 <>
            r.request_json->>'suite_manifest_sha256' THEN
                RAISE EXCEPTION 'holdout suite version is not pinned to this development manifest';
            END IF;
            SELECT * INTO existing FROM lab.holdout_reservations
             WHERE run_id=p_run_id AND request_key=p_request_key;
            IF FOUND THEN
                IF existing.candidate_experiment_id <> p_candidate_experiment_id
                   OR existing.trigger_kind <> p_trigger_kind OR existing.trigger_index <>
                   p_trigger_index THEN
                    RAISE EXCEPTION 'holdout idempotency key was reused with different identity';
                END IF;
                RETURN jsonb_build_object('reservation_id',existing.reservation_id,
                    'state',existing.state,'bit',CASE WHEN existing.state IN
                    ('passed','reverted') THEN existing.result_bit ELSE NULL END);
            END IF;
            IF r.state <> 'running' THEN RAISE EXCEPTION 'run is not active'; END IF;
            SELECT count(*) INTO registered_count FROM scorer.holdout_suite_tasks
             WHERE suite_id=cal.suite_id AND suite_version=cal.suite_version;
            IF registered_count <> version_row.task_count THEN
                RAISE EXCEPTION 'holdout suite registration is incomplete';
            END IF;
            SELECT * INTO exp FROM lab.experiments
             WHERE experiment_id=p_candidate_experiment_id AND run_id=p_run_id AND status='scored';
            IF NOT FOUND THEN RAISE EXCEPTION 'candidate is not a terminal scored run
            experiment'; END IF;
            IF p_trigger_kind='keep_interval' AND (
                exp.kind <> 'proposal' OR
                NOT EXISTS (
                    SELECT 1 FROM lab.experiment_records er
                     WHERE er.experiment_id=exp.experiment_id
                       AND er.experiment_json->'decision'->>'verdict' IN ('KEEP','KEEP_SIMPLER')
                )
            ) THEN
                RAISE EXCEPTION 'interval holdout requires the matching KEEP candidate';
            END IF;
            IF p_trigger_kind='run_end' AND NOT (
                (exp.kind='baseline' AND exp.baseline_name='robust_z') OR
                (exp.kind='proposal' AND EXISTS (
                    SELECT 1 FROM lab.experiment_records er
                     WHERE er.experiment_id=exp.experiment_id
                       AND er.experiment_json->'decision'->>'verdict' IN ('KEEP','KEEP_SIMPLER')
                ))
            ) THEN
                RAISE EXCEPTION 'run-end holdout requires the final KEEP or robust_z champion';
            END IF;
            IF p_trigger_kind='run_end' THEN
                SELECT * INTO end_intent FROM lab.holdout_run_end_intents
                 WHERE run_id=p_run_id;
                IF NOT FOUND OR end_intent.candidate_experiment_id IS DISTINCT FROM
                    p_candidate_experiment_id OR end_intent.candidate_sha256 IS DISTINCT FROM
                    exp.candidate_sha256 THEN
                    RAISE EXCEPTION 'run-end candidate differs from the durable final champion';
                END IF;
            END IF;
            IF p_trigger_kind='keep_interval' THEN
                SELECT count(*) INTO keep_count
                  FROM lab.experiments e
                  JOIN lab.experiment_records er USING (experiment_id)
                 WHERE e.run_id=p_run_id AND e.kind='proposal'
                   AND e.status='scored'
                   AND e.experiment_number <= exp.experiment_number
                   AND er.experiment_json->'decision'->>'verdict' IN ('KEEP','KEEP_SIMPLER');
                IF keep_count <> p_trigger_index OR keep_count % 10 <> 0 THEN
                    RAISE EXCEPTION 'interval holdout is allowed only after each tenth KEEP';
                END IF;
            END IF;
            SELECT * INTO approval FROM lab.holdout_approvals WHERE run_id=p_run_id;
            IF FOUND THEN
                ref_sha := approval.candidate_sha256;
                SELECT candidate_blob_sha256 INTO ref_blob FROM lab.holdout_reservations
                 WHERE reservation_id=approval.reservation_id;
            ELSE
                SELECT candidate_sha256,candidate_blob_sha256 INTO ref_sha,ref_blob FROM
                lab.experiments
                 WHERE run_id=p_run_id AND kind='baseline' AND baseline_name='robust_z';
                IF ref_sha IS NULL OR ref_blob IS NULL THEN RAISE EXCEPTION 'holdout
                reference champion is unavailable'; END IF;
            END IF;
            INSERT INTO lab.holdout_run_quotas(run_id,suite_id,suite_version,manifest_sha256,used)
            VALUES(p_run_id,cal.suite_id,cal.suite_version,version_row.manifest_sha256,0)
            ON CONFLICT(run_id) DO NOTHING;
            SELECT used INTO run_used FROM lab.holdout_run_quotas WHERE run_id=p_run_id FOR UPDATE;
            INSERT INTO lab.holdout_suite_quotas(suite_id,suite_version,used)
            VALUES(cal.suite_id,cal.suite_version,0) ON CONFLICT(suite_id,suite_version) DO NOTHING;
            SELECT used INTO suite_used FROM lab.holdout_suite_quotas
             WHERE suite_id=cal.suite_id AND suite_version=cal.suite_version FOR UPDATE;
            IF run_used >= 20 OR suite_used >= 100 THEN
                INSERT INTO lab.holdout_reservations(
                    reservation_id,run_id,request_key,suite_id,suite_version,manifest_sha256,
                    candidate_experiment_id,candidate_sha256,candidate_blob_sha256,
                    reference_sha256,reference_blob_sha256,trigger_kind,trigger_index,
                    epsilon,state)
                VALUES(p_reservation_id,p_run_id,p_request_key,cal.suite_id,cal.suite_version,
                    version_row.manifest_sha256,p_candidate_experiment_id,exp.candidate_sha256,
                    exp.candidate_blob_sha256,ref_sha,ref_blob,
                    p_trigger_kind,p_trigger_index,version_row.epsilon,'exhausted');
                RETURN jsonb_build_object(
                    'reservation_id',p_reservation_id,'state','exhausted','bit',NULL
                );
            END IF;
            UPDATE lab.holdout_run_quotas SET used=used+1 WHERE run_id=p_run_id;
            UPDATE lab.holdout_suite_quotas SET used=used+1
             WHERE suite_id=cal.suite_id AND suite_version=cal.suite_version;
            INSERT INTO lab.holdout_reservations(
                reservation_id,run_id,request_key,suite_id,suite_version,manifest_sha256,
                candidate_experiment_id,candidate_sha256,candidate_blob_sha256,
                reference_sha256,reference_blob_sha256,trigger_kind,trigger_index,epsilon)
            VALUES(p_reservation_id,p_run_id,p_request_key,cal.suite_id,cal.suite_version,
                version_row.manifest_sha256,p_candidate_experiment_id,exp.candidate_sha256,
                exp.candidate_blob_sha256,ref_sha,ref_blob,
                p_trigger_kind,p_trigger_index,version_row.epsilon);
            RETURN jsonb_build_object(
                'reservation_id',p_reservation_id,'state','reserved','bit',NULL
            );
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.read_holdout_bit(p_run_id uuid,p_reservation_id uuid)
        RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE item lab.holdout_reservations%ROWTYPE;
        BEGIN
            IF session_user <> 'swapp_lab_director' THEN RAISE EXCEPTION 'Director role
            required'; END IF;
            SELECT * INTO item FROM lab.holdout_reservations
             WHERE run_id=p_run_id AND reservation_id=p_reservation_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout receipt is unavailable'; END IF;
            RETURN jsonb_build_object('reservation_id',item.reservation_id,
                'run_id',item.run_id,
                'candidate_experiment_id',item.candidate_experiment_id,
                'trigger_kind',item.trigger_kind,'trigger_index',item.trigger_index,
                'state',item.state,
                'bit',CASE WHEN item.state IN ('passed','reverted') THEN item.result_bit ELSE
                NULL END);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.claim_holdout_reservation(
            p_reservation_id uuid,p_worker_pid integer,p_start_ticks bigint,p_boot_id text,
            p_unit text,p_invocation_id text,p_cgroup text
        )
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab,scorer AS $$
        DECLARE item lab.holdout_reservations%ROWTYPE; target_run uuid;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN RAISE EXCEPTION 'Scorer role
            required'; END IF;
            IF p_worker_pid <= 1 OR p_start_ticks <= 0 OR p_boot_id !~ '^[0-9a-f-]{36}$'
               OR p_invocation_id !~ '^[0-9a-f]{32}$'
               OR p_unit !~ '^swapp-ai-scientist-scorer-[0-9a-f]{32}[.]service$'
               OR p_cgroup !~ '^/.+[.]service$' OR p_cgroup NOT LIKE '%/' || p_unit THEN
                RAISE EXCEPTION 'holdout worker identity is invalid';
            END IF;
            SELECT run_id INTO target_run FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout reservation is unavailable'; END IF;
            PERFORM 1 FROM lab.runs WHERE run_id=target_run AND state='running' FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'run is no longer active for holdout claim'; END IF;
            SELECT * INTO item FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id FOR UPDATE;
            IF NOT FOUND OR item.run_id <> target_run THEN
                RAISE EXCEPTION 'holdout reservation identity changed';
            END IF;
            IF item.state='reserved' THEN
                IF p_unit <> 'swapp-ai-scientist-scorer-' ||
                replace(p_reservation_id::text,'-','') || '.service' THEN
                    RAISE EXCEPTION 'holdout worker unit is not bound to the reservation';
                END IF;
                UPDATE lab.holdout_reservations SET state='running',worker_pid=p_worker_pid,
                    worker_start_ticks=p_start_ticks,worker_boot_id=p_boot_id,worker_unit=p_unit,
                    worker_invocation_id=p_invocation_id,worker_cgroup=p_cgroup
                 WHERE reservation_id=p_reservation_id;
                item.state := 'running';
            ELSE
                RAISE EXCEPTION 'holdout reservation is not runnable';
            END IF;
            RETURN jsonb_build_object('reservation_id',item.reservation_id,'run_id',item.run_id,
                'suite_id',item.suite_id,'suite_version',item.suite_version,
                'manifest_sha256',item.manifest_sha256,'candidate_sha256',item.candidate_sha256,
                'candidate_blob_sha256',item.candidate_blob_sha256,
                'reference_sha256',item.reference_sha256,'reference_blob_sha256',item.reference_blob_sha256,
                'epsilon',item.epsilon,'worker_pid',p_worker_pid,'worker_start_ticks',p_start_ticks,
                'worker_boot_id',p_boot_id,'worker_unit',p_unit,'worker_invocation_id',p_invocation_id,
                'worker_cgroup',p_cgroup);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.publish_holdout_result(
            p_reservation_id uuid,p_delta double precision,p_result_json jsonb,
            p_result_sha256 text,p_worker_pid integer,p_start_ticks bigint,p_boot_id text,
            p_unit text,p_invocation_id text,p_cgroup text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab,scorer AS $$
        DECLARE item lab.holdout_reservations%ROWTYPE; run_item lab.runs%ROWTYPE;
        DECLARE target_run uuid; passed boolean;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN RAISE EXCEPTION 'Scorer role
            required'; END IF;
            SELECT run_id INTO target_run FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout reservation is unavailable'; END IF;
            SELECT * INTO run_item FROM lab.runs WHERE run_id=target_run FOR UPDATE;
            IF NOT FOUND OR run_item.state <> 'running' THEN
                RAISE EXCEPTION 'run is no longer active for holdout publication';
            END IF;
            SELECT * INTO item FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id FOR UPDATE;
            IF NOT FOUND OR item.state <> 'running' OR item.run_id <> target_run OR
               ROW(item.worker_pid,item.worker_start_ticks,item.worker_boot_id,item.worker_unit,
                   item.worker_invocation_id,item.worker_cgroup) IS DISTINCT FROM
               ROW(p_worker_pid,p_start_ticks,p_boot_id,p_unit,p_invocation_id,p_cgroup) THEN
                RAISE EXCEPTION 'holdout worker generation is stale';
            END IF;
            IF p_delta IS NULL OR p_delta::text IN ('NaN','Infinity','-Infinity')
               OR p_result_json IS NULL OR p_result_sha256 !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'holdout result is malformed';
            END IF;
            passed := p_delta >= -item.epsilon;
            INSERT INTO scorer.holdout_results(reservation_id,result_json,result_sha256,delta)
            VALUES(p_reservation_id,p_result_json,p_result_sha256,p_delta);
            UPDATE lab.holdout_reservations SET
                state=CASE WHEN passed THEN 'passed' ELSE 'reverted' END,
                result_bit=passed,result_sha256=p_result_sha256,completed_at=now()
             WHERE reservation_id=p_reservation_id;
            IF passed THEN
                INSERT INTO lab.holdout_approvals(run_id,candidate_sha256,reservation_id)
                VALUES(item.run_id,item.candidate_sha256,p_reservation_id)
                ON CONFLICT(run_id) DO UPDATE SET candidate_sha256=excluded.candidate_sha256,
                    reservation_id=excluded.reservation_id,updated_at=now();
            END IF;
            RETURN jsonb_build_object('state',CASE WHEN passed THEN 'passed' ELSE 'reverted'
            END,'bit',passed);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lab.fail_holdout_reservation(
            p_reservation_id uuid,p_error_code text,p_worker_pid integer,p_start_ticks bigint,
            p_boot_id text,p_unit text,p_invocation_id text,p_cgroup text
        )
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab,scorer AS $$
        DECLARE item lab.holdout_reservations%ROWTYPE; target_run uuid;
        BEGIN
            IF session_user <> 'swapp_lab_scorer' THEN RAISE EXCEPTION 'Scorer role
            required'; END IF;
            IF p_error_code IS NULL OR p_error_code !~ '^[a-z0-9_]{1,48}$' THEN
                RAISE EXCEPTION 'holdout error code is invalid';
            END IF;
            SELECT run_id INTO target_run FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout reservation is unavailable'; END IF;
            PERFORM 1 FROM lab.runs WHERE run_id=target_run FOR UPDATE;
            SELECT * INTO item FROM lab.holdout_reservations
             WHERE reservation_id=p_reservation_id FOR UPDATE;
            IF NOT FOUND OR item.state <> 'running' OR item.run_id <> target_run OR
               ROW(item.worker_pid,item.worker_start_ticks,item.worker_boot_id,item.worker_unit,
                   item.worker_invocation_id,item.worker_cgroup) IS DISTINCT FROM
               ROW(p_worker_pid,p_start_ticks,p_boot_id,p_unit,p_invocation_id,p_cgroup) THEN
                RAISE EXCEPTION 'holdout worker generation is stale';
            END IF;
            UPDATE lab.holdout_reservations SET
            state='failed',error_code=p_error_code,completed_at=now()
             WHERE reservation_id=p_reservation_id AND state='running';
            IF NOT FOUND THEN RAISE EXCEPTION 'holdout reservation cannot be failed'; END IF;
        END;
        $$
        """
    )
    for signature in (
        "lab.reserve_holdout_check(uuid,uuid,text,text,text,integer)",
        "lab.read_holdout_bit(uuid,uuid)",
        "lab.claim_holdout_reservation(uuid,integer,bigint,text,text,text,text)",
        "lab.publish_holdout_result(uuid,double precision,jsonb,text,integer,bigint,"
        "text,text,text,text)",
        "lab.fail_holdout_reservation(uuid,text,integer,bigint,text,text,text,text)",
        "lab.guard_holdout_run_end_no_more_experiments()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "lab.reserve_holdout_check(uuid,uuid,text,text,text,integer) TO "
        "swapp_lab_director"
    )
    op.execute("GRANT EXECUTE ON FUNCTION lab.read_holdout_bit(uuid,uuid) TO swapp_lab_director")
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "lab.claim_holdout_reservation(uuid,integer,bigint,text,text,text,text) "
        "TO swapp_lab_scorer"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.publish_holdout_result(uuid,double "
        "precision,jsonb,text,integer,bigint,text,text,text,text) TO swapp_lab_scorer"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.fail_holdout_reservation(uuid,text,integer,bigint,"
        "text,text,text,text) "
        "TO swapp_lab_scorer"
    )


def downgrade() -> None:
    """Remove holdout objects after active reservations are drained."""
    for name, signature in (
        ("holdout_suite_is_registered", "uuid"),
        ("fail_holdout_reservation", "uuid,text"),
        ("publish_holdout_result", "uuid,double precision,jsonb,text"),
        (
            "fail_holdout_reservation",
            "uuid,text,integer,bigint,text,text,text,text",
        ),
        (
            "publish_holdout_result",
            "uuid,double precision,jsonb,text,integer,bigint,text,text,text,text",
        ),
        ("claim_holdout_reservation", "uuid,integer,bigint,text,text,text,text"),
        ("read_holdout_bit", "uuid,uuid"),
        ("read_holdout_request", "uuid,text,text,text,integer"),
        ("reserve_holdout_check", "uuid,uuid,text,text,text,integer"),
    ):
        op.execute(f"DROP FUNCTION IF EXISTS lab.{name}({signature})")
    op.execute("DROP TRIGGER holdout_run_end_blocks_experiments ON lab.experiments")
    op.execute("DROP FUNCTION lab.guard_holdout_run_end_no_more_experiments()")
    op.execute("DROP TRIGGER holdout_reservation_guard ON lab.holdout_reservations")
    op.execute("DROP FUNCTION lab.guard_holdout_reservation()")
    op.execute("DROP TRIGGER holdout_task_profile_guard ON scorer.holdout_suite_tasks")
    op.execute("DROP FUNCTION lab.guard_holdout_profile_binding()")
    op.execute("DROP FUNCTION lab.register_holdout_run_end_intent(uuid,jsonb)")
    for table, schema in (
        ("holdout_results", "scorer"),
        ("holdout_run_end_intents", "lab"),
        ("holdout_approvals", "lab"),
        ("holdout_reservations", "lab"),
        ("holdout_run_quotas", "lab"),
        ("holdout_suite_quotas", "lab"),
        ("holdout_suite_tasks", "scorer"),
        ("holdout_suite_versions", "scorer"),
    ):
        op.drop_table(table, schema=schema)
