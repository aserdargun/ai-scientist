"""Typed SQLAlchemy tables shared by migrations and service code."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")
metadata = MetaData()

runs = Table(
    "runs",
    metadata,
    Column("run_id", Uuid(as_uuid=True), primary_key=True),
    Column("origin", String(32), nullable=False),
    Column("owner_id", String(128), nullable=False),
    Column("external_task_id", String(37)),
    Column("external_run_id", String(36)),
    Column("external_action_id", String(39)),
    Column("idempotency_key", String(128), nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("request_json", JSON_DOCUMENT, nullable=False),
    Column("state", String(24), nullable=False, server_default="queued"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("report_sha256", String(64)),
    Column("stop_requested", Boolean, nullable=False, server_default="false"),
    Column("task_plan_sha256", String(64)),
    Column("task_plan_count", Integer),
    CheckConstraint(
        "state in ('queued','running','stop_requested','completed','failed','stopped')",
        name="ck_runs_state",
    ),
    CheckConstraint("origin in ('local','aos')", name="ck_runs_origin"),
    CheckConstraint(
        "(external_task_id is null and external_run_id is null and external_action_id is null) "
        "or (origin = 'aos' and external_task_id is not null "
        "and external_run_id is not null and external_action_id is not null "
        "and length(external_task_id) = 37 "
        "and external_task_id like 'task-%' and length(external_run_id) = 36 "
        "and external_run_id like 'run-%' and length(external_action_id) = 39 "
        "and external_action_id like 'action-%')",
        name="ck_runs_external_identity",
    ),
    CheckConstraint(
        "(task_plan_sha256 is null and task_plan_count is null) or "
        "(length(task_plan_sha256) = 64 and task_plan_count >= 0)",
        name="ck_runs_task_plan_seal",
    ),
    schema="lab",
)
Index(
    "uq_runs_idempotency_owner",
    runs.c.origin,
    runs.c.owner_id,
    runs.c.idempotency_key,
    unique=True,
)
Index(
    "uq_runs_external_action_owner",
    runs.c.origin,
    runs.c.owner_id,
    runs.c.external_action_id,
    unique=True,
    postgresql_where=runs.c.external_action_id.is_not(None),
    sqlite_where=runs.c.external_action_id.is_not(None),
)

run_events = Table(
    "run_events",
    metadata,
    Column("event_id", Uuid(as_uuid=True), primary_key=True),
    Column("run_id", Uuid(as_uuid=True), ForeignKey("lab.runs.run_id", ondelete="CASCADE")),
    Column("event_type", String(64), nullable=False),
    Column("event_json", JSON_DOCUMENT, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    schema="lab",
)

reports = Table(
    "reports",
    metadata,
    Column(
        "run_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("report_sha256", String(64), nullable=False),
    Column("report_json", JSON_DOCUMENT, nullable=False),
    Column("verified_at", DateTime(timezone=True), nullable=False),
    schema="lab",
)

experiments = Table(
    "experiments",
    metadata,
    Column("experiment_id", String(128), primary_key=True),
    Column(
        "run_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("sequence", Integer, nullable=False),
    Column("experiment_number", Integer),
    Column("kind", String(16), nullable=False),
    Column("baseline_name", String(32)),
    Column(
        "parent_experiment_id",
        String(128),
        ForeignKey("lab.experiments.experiment_id", ondelete="RESTRICT"),
    ),
    Column("candidate_sha256", String(64), nullable=False),
    Column("candidate_blob_sha256", String(64), nullable=False),
    Column("inputs_sha256", String(64), nullable=False),
    Column("move_type", String(32), nullable=False),
    Column("system", String(2), nullable=False),
    Column("hypothesis", String(2_000), nullable=False),
    Column("predicted_delta", Float),
    Column("calibration_sha256", String(64)),
    Column("proposal_json", JSON_DOCUMENT, nullable=False),
    Column("status", String(32), nullable=False, server_default="proposed"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("run_id", "sequence", name="uq_experiments_run_sequence"),
    UniqueConstraint("run_id", "experiment_number", name="uq_experiments_run_number"),
    CheckConstraint("sequence >= 0", name="ck_experiments_sequence"),
    CheckConstraint(
        "(kind = 'baseline' and experiment_number is null and baseline_name is not null "
        "and baseline_name in "
        "('robust_z','iforest','ecod_train_frozen')) or "
        "(kind = 'proposal' and experiment_number is not null and experiment_number >= 1 "
        "and baseline_name is null)",
        name="ck_experiments_kind_identity",
    ),
    CheckConstraint("length(candidate_sha256) = 64", name="ck_experiments_candidate_sha"),
    CheckConstraint("length(candidate_blob_sha256) = 64", name="ck_experiments_blob_sha"),
    CheckConstraint("length(inputs_sha256) = 64", name="ck_experiments_inputs_sha"),
    CheckConstraint("system in ('S1','S2')", name="ck_experiments_system"),
    CheckConstraint(
        "status in ('proposed','primary_running','awaiting_confirmation',"
        "'confirmation_running','scored','crashed','abandoned','rejected')",
        name="ck_experiments_status",
    ),
    CheckConstraint(
        "predicted_delta is null or predicted_delta between -4 and 4",
        name="ck_experiments_predicted_delta",
    ),
    CheckConstraint(
        "calibration_sha256 is null or length(calibration_sha256) = 64",
        name="ck_experiments_calibration_sha",
    ),
    schema="lab",
)

experiment_records = Table(
    "experiment_records",
    metadata,
    Column(
        "experiment_id",
        String(128),
        ForeignKey("lab.experiments.experiment_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("experiment_json", JSON_DOCUMENT, nullable=False),
    Column("experiment_sha256", String(64), nullable=False),
    Column("experiment_blob_sha256", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("length(experiment_sha256) = 64", name="ck_experiment_record_sha"),
    CheckConstraint("length(experiment_blob_sha256) = 64", name="ck_experiment_record_blob_sha"),
    schema="lab",
)

trajectory_records = Table(
    "trajectory_records",
    metadata,
    Column(
        "experiment_id",
        String(128),
        ForeignKey("lab.experiments.experiment_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("trajectory_json", JSON_DOCUMENT, nullable=False),
    Column("trajectory_sha256", String(64), nullable=False),
    Column("trajectory_blob_sha256", String(64), nullable=False),
    Column("messages_blob_sha256", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("length(trajectory_sha256) = 64", name="ck_trajectory_record_sha"),
    CheckConstraint("length(trajectory_blob_sha256) = 64", name="ck_trajectory_record_blob_sha"),
    CheckConstraint("length(messages_blob_sha256) = 64", name="ck_trajectory_messages_blob_sha"),
    schema="lab",
)

baseline_calibrations = Table(
    "baseline_calibrations",
    metadata,
    Column(
        "run_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("suite_id", String(128), nullable=False),
    Column("suite_version", Integer, nullable=False),
    Column("calibration_sha256", String(64), nullable=False),
    Column("blob_sha256", String(64), nullable=False),
    Column("task_count", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("suite_version > 0", name="ck_calibration_suite_version"),
    CheckConstraint("length(calibration_sha256) = 64", name="ck_calibration_sha"),
    CheckConstraint("length(blob_sha256) = 64", name="ck_calibration_blob_sha"),
    CheckConstraint("task_count > 0", name="ck_calibration_task_count"),
    schema="lab",
)

baseline_operations = Table(
    "baseline_operations",
    metadata,
    Column(
        "run_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("request_sha256", String(64), nullable=False),
    Column("suite_manifest_sha256", String(64), nullable=False),
    Column("harness_sha256", String(64), nullable=False),
    Column("image_sha256", String(64), nullable=False),
    Column("calibration_sha256", String(64), nullable=False),
    Column("task_plan_sha256", String(64), nullable=False),
    Column("task_plan_count", Integer, nullable=False),
    Column("budget_receipt", JSON_DOCUMENT, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("length(request_sha256) = 64", name="ck_baseline_operation_request_sha"),
    CheckConstraint(
        "length(suite_manifest_sha256) = 64", name="ck_baseline_operation_manifest_sha"
    ),
    CheckConstraint("length(harness_sha256) = 64", name="ck_baseline_operation_harness_sha"),
    CheckConstraint("length(image_sha256) = 64", name="ck_baseline_operation_image_sha"),
    CheckConstraint(
        "length(calibration_sha256) = 64", name="ck_baseline_operation_calibration_sha"
    ),
    CheckConstraint("length(task_plan_sha256) = 64", name="ck_baseline_operation_plan_sha"),
    CheckConstraint("task_plan_count > 0", name="ck_baseline_operation_plan_count"),
    schema="lab",
)

dataset_labels = Table(
    "dataset_labels",
    metadata,
    Column("dataset_id", String(128), primary_key=True),
    Column("split_id", String(128), primary_key=True),
    Column("session_id", String(256), primary_key=True),
    Column("sample_index", Integer, primary_key=True),
    Column("is_anomaly", Boolean, nullable=False),
    ForeignKeyConstraint(
        ("dataset_id", "split_id", "session_id"),
        (
            "scorer.dataset_profiles.dataset_id",
            "scorer.dataset_profiles.split_id",
            "scorer.dataset_profiles.session_id",
        ),
        ondelete="CASCADE",
    ),
    schema="scorer",
)

dataset_task_semantics = Table(
    "dataset_task_semantics",
    metadata,
    Column("dataset_id", String(128), primary_key=True),
    Column("split_id", String(128), primary_key=True),
    Column("session_id", String(256), primary_key=True),
    Column("task_family", String(16), nullable=False),
    Column("sampling_s", Integer, nullable=False),
    Column("evaluation_times_json", JSON_DOCUMENT, nullable=False),
    Column("masked_samples_json", JSON_DOCUMENT, nullable=False),
    Column("failure_windows_json", JSON_DOCUMENT, nullable=False),
    Column("semantics_sha256", String(64), nullable=False),
    ForeignKeyConstraint(
        ("dataset_id", "split_id", "session_id"),
        (
            "scorer.dataset_profiles.dataset_id",
            "scorer.dataset_profiles.split_id",
            "scorer.dataset_profiles.session_id",
        ),
        ondelete="CASCADE",
    ),
    CheckConstraint("task_family in ('EVT','PDM','NRM')", name="ck_dataset_task_family"),
    CheckConstraint("sampling_s > 0", name="ck_dataset_task_sampling"),
    CheckConstraint("length(semantics_sha256) = 64", name="ck_dataset_task_semantics_sha"),
    schema="scorer",
)

dataset_profiles = Table(
    "dataset_profiles",
    metadata,
    Column("dataset_id", String(128), primary_key=True),
    Column("split_id", String(128), primary_key=True),
    Column("session_id", String(256), primary_key=True),
    Column("sample_count", Integer, nullable=False),
    Column("sliding_window", Integer, nullable=False),
    Column("profile_sha256", String(64), nullable=False),
    Column("task_family", String(16), nullable=False, server_default="EVT"),
    Column("visibility", String(16), nullable=False, server_default="sealed"),
    CheckConstraint("task_family in ('EVT','PDM','NRM')", name="ck_dataset_profile_family"),
    CheckConstraint("sample_count > 0", name="ck_dataset_profile_sample_count"),
    CheckConstraint("sliding_window > 0", name="ck_dataset_profile_window"),
    CheckConstraint(
        "visibility in ('dev','holdout','sealed')", name="ck_dataset_profile_visibility"
    ),
    schema="scorer",
)

run_tasks = Table(
    "run_tasks",
    metadata,
    Column(
        "run_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("experiment_id", String(128), primary_key=True),
    Column("evaluation_kind", String(24), primary_key=True),
    Column("task_id", String(128), primary_key=True),
    Column("seed", Integer, primary_key=True),
    Column("candidate_sha256", String(64), nullable=False),
    Column("dataset_id", String(128), nullable=False),
    Column("split_id", String(128), nullable=False),
    Column("session_id", String(256), nullable=False),
    ForeignKeyConstraint(
        ("dataset_id", "split_id", "session_id"),
        (
            "scorer.dataset_profiles.dataset_id",
            "scorer.dataset_profiles.split_id",
            "scorer.dataset_profiles.session_id",
        ),
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "evaluation_kind in ('baseline','primary','confirmation')", name="ck_run_tasks_eval"
    ),
    CheckConstraint("seed >= 0", name="ck_run_tasks_seed"),
    CheckConstraint("length(candidate_sha256) = 64", name="ck_run_tasks_candidate_sha"),
    schema="scorer",
)

task_scores = Table(
    "task_scores",
    metadata,
    Column(
        "run_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("experiment_id", String(128), primary_key=True),
    Column("evaluation_kind", String(24), primary_key=True),
    Column("task_id", String(128), primary_key=True),
    Column("seed", Integer, primary_key=True),
    Column("score", JSON_DOCUMENT, nullable=False),
    Column("guard_results", JSON_DOCUMENT, nullable=False),
    Column("score_job_id", Uuid(as_uuid=True), ForeignKey("scorer.score_jobs.job_id")),
    Column("claim_token", String(128)),
    Column("worker_invocation_id", String(32)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ("run_id", "experiment_id", "evaluation_kind", "task_id", "seed"),
        (
            "scorer.run_tasks.run_id",
            "scorer.run_tasks.experiment_id",
            "scorer.run_tasks.evaluation_kind",
            "scorer.run_tasks.task_id",
            "scorer.run_tasks.seed",
        ),
        ondelete="CASCADE",
    ),
    schema="scorer",
)

task_completions = Table(
    "task_completions",
    metadata,
    Column("run_id", Uuid(as_uuid=True), primary_key=True),
    Column("experiment_id", String(128), primary_key=True),
    Column("evaluation_kind", String(24), primary_key=True),
    Column("task_id", String(128), primary_key=True),
    Column("seed", Integer, primary_key=True),
    Column("completion_kind", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ("run_id", "experiment_id", "evaluation_kind", "task_id", "seed"),
        (
            "scorer.run_tasks.run_id",
            "scorer.run_tasks.experiment_id",
            "scorer.run_tasks.evaluation_kind",
            "scorer.run_tasks.task_id",
            "scorer.run_tasks.seed",
        ),
        ondelete="CASCADE",
    ),
    CheckConstraint("completion_kind in ('scored','terminal')", name="ck_task_completion_kind"),
    schema="scorer",
)

task_terminal_outcomes = Table(
    "task_terminal_outcomes",
    metadata,
    Column("run_id", Uuid(as_uuid=True), primary_key=True),
    Column("experiment_id", String(128), primary_key=True),
    Column("evaluation_kind", String(24), primary_key=True),
    Column("task_id", String(128), primary_key=True),
    Column("seed", Integer, primary_key=True),
    Column("candidate_sha256", String(64), nullable=False),
    Column("outcome_code", String(32), nullable=False),
    Column("producer_role", String(32), nullable=False),
    Column("score_job_id", Uuid(as_uuid=True), ForeignKey("scorer.score_jobs.job_id")),
    Column("claim_token", String(128)),
    Column("worker_invocation_id", String(32)),
    Column("recovery_invocation_id", String(32)),
    Column("admitted_generation", Integer),
    Column("execution_sha256", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ("run_id", "experiment_id", "evaluation_kind", "task_id", "seed"),
        (
            "scorer.run_tasks.run_id",
            "scorer.run_tasks.experiment_id",
            "scorer.run_tasks.evaluation_kind",
            "scorer.run_tasks.task_id",
            "scorer.run_tasks.seed",
        ),
        ondelete="CASCADE",
    ),
    CheckConstraint(
        "outcome_code in ('guard_rejected','candidate_rejected','candidate_crash',"
        "'candidate_timeout','scorer_error','cancelled','infrastructure_unattempted')",
        name="ck_task_terminal_outcome_code",
    ),
    CheckConstraint(
        "recovery_invocation_id is null or length(recovery_invocation_id) = 32",
        name="ck_task_terminal_recovery_invocation",
    ),
    CheckConstraint("length(candidate_sha256) = 64", name="ck_terminal_candidate_sha"),
    CheckConstraint(
        "(admitted_generation is null and execution_sha256 is null) or "
        "(admitted_generation > 0 and length(execution_sha256) = 64)",
        name="ck_terminal_execution_owner_pair",
    ),
    CheckConstraint("execution_sha256 ~ '^[0-9a-f]{64}$'", name="ck_terminal_execution_sha").ddl_if(
        dialect="postgresql"
    ),
    schema="scorer",
)

director_run_owners = Table(
    "director_run_owners",
    metadata,
    Column(
        "run_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("payload_sha256", String(64), nullable=False),
    Column("worker_pid", Integer, nullable=False),
    Column("worker_start_ticks", BigInteger, nullable=False),
    Column("worker_boot_id", String(36), nullable=False),
    Column("worker_unit", String(255), nullable=False),
    Column("worker_invocation_id", String(32), nullable=False),
    Column("worker_cgroup", Text, nullable=False),
    Column("claimed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("worker_pid > 1", name="ck_director_owner_pid"),
    CheckConstraint("worker_start_ticks > 0", name="ck_director_owner_start_ticks"),
    CheckConstraint("length(payload_sha256) = 64", name="ck_director_owner_payload_sha"),
    CheckConstraint("worker_boot_id ~ '^[0-9a-f-]{36}$'", name="ck_director_owner_boot_id").ddl_if(
        dialect="postgresql"
    ),
    CheckConstraint(
        "worker_invocation_id ~ '^[0-9a-f]{32}$'", name="ck_director_owner_invocation"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "worker_unit ~ '^[A-Za-z0-9_.@-]+[.]service$'", name="ck_director_owner_unit"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint("worker_cgroup ~ '^/.+[.]service$'", name="ck_director_owner_cgroup").ddl_if(
        dialect="postgresql"
    ),
    schema="lab",
)

director_recoveries = Table(
    "director_recoveries",
    metadata,
    Column("recovery_id", Uuid(as_uuid=True), primary_key=True),
    Column(
        "run_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("request_sha256", String(64), nullable=False),
    Column("action", String(24), nullable=False),
    Column("observed_run_state", String(24), nullable=False),
    Column("owner_pid", Integer),
    Column("owner_start_ticks", BigInteger),
    Column("owner_boot_id", String(36)),
    Column("owner_unit", String(255)),
    Column("owner_invocation_id", String(32)),
    Column("owner_cgroup", Text),
    Column("state", String(16), nullable=False, server_default="started"),
    Column("result_json", JSON_DOCUMENT),
    Column("result_sha256", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("action = 'stop_and_finalize'", name="ck_director_recovery_action"),
    CheckConstraint(
        "state in ('started','pending','completed','failed')", name="ck_director_recovery_state"
    ),
    CheckConstraint("length(request_sha256) = 64", name="ck_director_recovery_request_sha"),
    CheckConstraint(
        "observed_run_state in ('running','stop_requested','completed','failed','stopped')",
        name="ck_director_recovery_observed_state",
    ),
    CheckConstraint(
        "(owner_pid is null and owner_start_ticks is null and owner_boot_id is null and "
        "owner_unit is null and owner_invocation_id is null and owner_cgroup is null) or "
        "(owner_pid is not null and owner_start_ticks is not null and "
        "owner_boot_id is not null and owner_unit is not null and "
        "owner_invocation_id is not null and owner_cgroup is not null and "
        "owner_pid > 1 and owner_start_ticks > 0 and length(owner_boot_id) = 36 and "
        "length(owner_unit) > 0 and length(owner_invocation_id) = 32 and length(owner_cgroup) > 0)",
        name="ck_director_recovery_owner_identity",
    ),
    CheckConstraint(
        "(result_json is null and result_sha256 is null) or "
        "(result_json is not null and result_sha256 is not null and length(result_sha256) = 64)",
        name="ck_director_recovery_result_digest",
    ),
    UniqueConstraint("run_id", "recovery_id", name="uq_director_recovery_run_id"),
    schema="lab",
)

director_execution_contracts = Table(
    "director_execution_contracts",
    metadata,
    Column(
        "run_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("payload_sha256", String(64), nullable=False),
    Column("execution_json", JSON_DOCUMENT, nullable=False),
    Column("execution_sha256", String(64), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("deadline_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("run_id", "execution_sha256", name="uq_director_execution_contract_digest"),
    CheckConstraint(
        "payload_sha256 ~ '^[0-9a-f]{64}$'", name="ck_director_execution_payload_sha"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint("execution_sha256 ~ '^[0-9a-f]{64}$'", name="ck_director_execution_sha").ddl_if(
        dialect="postgresql"
    ),
    CheckConstraint("deadline_at > started_at", name="ck_director_execution_deadline"),
    schema="lab",
)

director_owner_generations = Table(
    "director_owner_generations",
    metadata,
    Column("run_id", Uuid(as_uuid=True), nullable=False),
    Column("generation", Integer, nullable=False),
    Column("execution_sha256", String(64), nullable=False),
    Column("worker_pid", Integer, nullable=False),
    Column("worker_start_ticks", BigInteger, nullable=False),
    Column("worker_boot_id", String(36), nullable=False),
    Column("worker_unit", String(255), nullable=False),
    Column("worker_invocation_id", String(32), nullable=False),
    Column("worker_cgroup", Text, nullable=False),
    Column("claimed_at", DateTime(timezone=True), nullable=False),
    Column("restart_id", Uuid(as_uuid=True)),
    ForeignKeyConstraint(
        ["run_id"], ["lab.director_execution_contracts.run_id"], ondelete="CASCADE"
    ),
    ForeignKeyConstraint(
        ["run_id", "execution_sha256"],
        [
            "lab.director_execution_contracts.run_id",
            "lab.director_execution_contracts.execution_sha256",
        ],
        name="fk_director_generation_contract",
    ),
    PrimaryKeyConstraint("run_id", "generation"),
    UniqueConstraint(
        "run_id", "worker_invocation_id", name="uq_director_owner_generation_invocation"
    ),
    UniqueConstraint(
        "run_id",
        "generation",
        "execution_sha256",
        name="uq_director_generation_execution_pair",
    ),
    CheckConstraint("generation > 0", name="ck_director_owner_generation_positive"),
    CheckConstraint(
        "execution_sha256 ~ '^[0-9a-f]{64}$'", name="ck_director_generation_execution_sha"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "worker_pid > 1 and worker_start_ticks > 0", name="ck_director_generation_process"
    ),
    CheckConstraint(
        "worker_boot_id ~ '^[0-9a-f-]{36}$'", name="ck_director_generation_boot"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "worker_invocation_id ~ '^[0-9a-f]{32}$'",
        name="ck_director_generation_invocation",
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "worker_unit ~ '^[A-Za-z0-9_.@-]+[.]service$'", name="ck_director_generation_unit"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "worker_cgroup ~ '^/.+[.]service$'", name="ck_director_generation_cgroup"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "worker_cgroup like '%/' || worker_unit", name="ck_director_generation_unit_cgroup"
    ),
    CheckConstraint(
        "(generation = 1 and restart_id is null) or (generation > 1 and restart_id is not null)",
        name="ck_director_generation_restart",
    ),
    schema="lab",
)

director_execution_control = Table(
    "director_execution_control",
    metadata,
    Column(
        "run_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.director_execution_contracts.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("current_generation", Integer, nullable=False),
    Column("mode", String(16), nullable=False, server_default="active"),
    Column("restart_id", Uuid(as_uuid=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["run_id", "current_generation"],
        ["lab.director_owner_generations.run_id", "lab.director_owner_generations.generation"],
    ),
    CheckConstraint("current_generation > 0", name="ck_director_control_generation"),
    CheckConstraint("mode in ('active','reconciling')", name="ck_director_control_mode"),
    CheckConstraint(
        "(mode = 'active' and restart_id is null) or "
        "(mode = 'reconciling' and restart_id is not null)",
        name="ck_director_control_restart",
    ),
    schema="lab",
)

director_restart_requests = Table(
    "director_restart_requests",
    metadata,
    Column("restart_id", Uuid(as_uuid=True), primary_key=True),
    Column("run_id", Uuid(as_uuid=True), nullable=False),
    Column("expected_generation", Integer, nullable=False),
    Column("request_sha256", String(64), nullable=False),
    Column("purpose", String(32), nullable=False),
    Column("request_json", JSON_DOCUMENT, nullable=False),
    Column("execution_sha256", String(64), nullable=False),
    Column("state", String(16), nullable=False, server_default="pending"),
    Column("observation_sha256", String(64)),
    Column("claimant_generation", Integer),
    Column("result_sha256", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["run_id", "expected_generation"],
        ["lab.director_owner_generations.run_id", "lab.director_owner_generations.generation"],
    ),
    UniqueConstraint(
        "run_id",
        "expected_generation",
        "request_sha256",
        name="uq_director_restart_request_identity",
    ),
    CheckConstraint(
        "request_sha256 ~ '^[0-9a-f]{64}$'", name="ck_director_restart_request_sha"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "execution_sha256 ~ '^[0-9a-f]{64}$'", name="ck_director_restart_execution_sha"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "purpose in ('continue','stop_and_finalize')", name="ck_director_restart_purpose"
    ),
    CheckConstraint(
        "state in ('pending','drained','claimed','rejected')", name="ck_director_restart_state"
    ),
    schema="lab",
)

director_stop_closures = Table(
    "director_stop_closures",
    metadata,
    Column(
        "recovery_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.director_recoveries.recovery_id"),
        primary_key=True,
    ),
    Column("run_id", Uuid(as_uuid=True), ForeignKey("lab.runs.run_id"), nullable=False),
    Column("expected_generation", Integer, nullable=False),
    Column("execution_sha256", Text, nullable=False),
    Column("owner_json", JSON_DOCUMENT, nullable=False),
    Column("state", Text, nullable=False, server_default="pending"),
    Column("recovery_invocation", Text),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.clock_timestamp(),
    ),
    UniqueConstraint("run_id"),
    CheckConstraint("state in ('pending','drained')"),
    schema="lab",
)

director_empty_baseline_stop_evidence = Table(
    "director_empty_baseline_stop_evidence",
    metadata,
    Column(
        "recovery_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.director_stop_closures.recovery_id"),
        primary_key=True,
    ),
    Column("phase", Text, primary_key=True),
    Column("evidence_json", JSON_DOCUMENT, nullable=False),
    Column("evidence_sha256", Text, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    ),
    CheckConstraint("phase in ('register','seal','retire')"),
    CheckConstraint(
        "evidence_sha256=encode(sha256(convert_to(evidence_json::text,'UTF8')),'hex')"
    ).ddl_if(dialect="postgresql"),
    schema="lab",
)


director_stopped_proposals = Table(
    "director_stopped_proposals",
    metadata,
    Column(
        "recovery_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.director_stop_closures.recovery_id"),
        primary_key=True,
    ),
    Column("experiment_id", Text, ForeignKey("lab.experiments.experiment_id"), nullable=False),
    Column("proposal_json", JSON_DOCUMENT, nullable=False),
    Column("proposal_sha256", Text, nullable=False),
    Column("reservation_sha256", Text, nullable=False),
    Column("reconciled_sha256", Text),
    Column("calibration_sha256", Text, nullable=False),
    UniqueConstraint("experiment_id"),
    CheckConstraint("proposal_sha256 ~ '^[0-9a-f]{64}$'").ddl_if(dialect="postgresql"),
    CheckConstraint("reservation_sha256 ~ '^[0-9a-f]{64}$'").ddl_if(dialect="postgresql"),
    CheckConstraint("reconciled_sha256 ~ '^[0-9a-f]{64}$'").ddl_if(dialect="postgresql"),
    CheckConstraint("calibration_sha256 ~ '^[0-9a-f]{64}$'").ddl_if(dialect="postgresql"),
    schema="lab",
)


director_stop_job_drains = Table(
    "director_stop_job_drains",
    metadata,
    Column(
        "recovery_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.director_stop_closures.recovery_id"),
        primary_key=True,
    ),
    Column("job_id", Uuid(as_uuid=True), ForeignKey("scorer.score_jobs.job_id"), primary_key=True),
    Column("original_job", JSON_DOCUMENT, nullable=False),
    Column("recovery_invocation", Text, nullable=False),
    Column(
        "observed_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.clock_timestamp(),
    ),
    CheckConstraint("recovery_invocation ~ '^[0-9a-f]{32}$'").ddl_if(dialect="postgresql"),
    schema="lab",
)

score_jobs = Table(
    "score_jobs",
    metadata,
    Column("job_id", Uuid(as_uuid=True), primary_key=True),
    Column("run_id", Uuid(as_uuid=True), nullable=False),
    Column("experiment_id", String(128), nullable=False),
    Column("evaluation_kind", String(24), nullable=False),
    Column("task_id", String(128), nullable=False),
    Column("seed", Integer, nullable=False),
    Column("candidate_sha256", String(64), nullable=False),
    Column("artifact_sha256", String(64), nullable=False),
    Column("admitted_generation", Integer),
    Column("execution_sha256", String(64)),
    Column("state", String(16), nullable=False, server_default="queued"),
    Column("attempt", Integer, nullable=False, server_default="0"),
    Column("claimed_by", String(128)),
    Column("lease_until", DateTime(timezone=True)),
    Column("claim_unit", String(128)),
    Column("claim_invocation_id", String(32)),
    Column("error_code", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ("run_id", "experiment_id", "evaluation_kind", "task_id", "seed"),
        (
            "scorer.run_tasks.run_id",
            "scorer.run_tasks.experiment_id",
            "scorer.run_tasks.evaluation_kind",
            "scorer.run_tasks.task_id",
            "scorer.run_tasks.seed",
        ),
        ondelete="CASCADE",
    ),
    UniqueConstraint(
        "run_id",
        "experiment_id",
        "evaluation_kind",
        "task_id",
        "seed",
        name="uq_score_jobs_task",
    ),
    CheckConstraint(
        "state in ('queued','running','completed','failed','cancelled')", name="ck_score_jobs_state"
    ),
    CheckConstraint("attempt >= 0", name="ck_score_jobs_attempt"),
    CheckConstraint("length(candidate_sha256) = 64", name="ck_score_jobs_candidate_sha"),
    CheckConstraint("length(artifact_sha256) = 64", name="ck_score_jobs_artifact_sha"),
    CheckConstraint(
        "(admitted_generation is null and execution_sha256 is null) or "
        "(admitted_generation > 0 and length(execution_sha256) = 64)",
        name="ck_score_jobs_execution_owner_pair",
    ),
    CheckConstraint(
        "execution_sha256 ~ '^[0-9a-f]{64}$'", name="ck_score_jobs_execution_sha"
    ).ddl_if(dialect="postgresql"),
    schema="scorer",
)
Index("ix_score_jobs_ready", score_jobs.c.state, score_jobs.c.lease_until, score_jobs.c.created_at)

care_baseline_calibration_jobs = Table(
    "care_baseline_calibration_jobs",
    metadata,
    Column("calibration_id", Uuid(as_uuid=True), primary_key=True),
    Column("suite_id", String(128), nullable=False),
    Column("suite_version", Integer, nullable=False),
    Column("suite_manifest_sha256", String(64), nullable=False),
    Column("calibration_manifest_sha256", String(64), nullable=False),
    Column("development_manifest_sha256", String(64), nullable=False),
    Column("epsilon", Float, nullable=False),
    Column("source_manifest_sha256", String(64), nullable=False),
    Column("source_archive_sha256", String(64), nullable=False),
    Column("task_weight_policy", String(64), nullable=False),
    Column("task_weight_policy_sha256", String(64), nullable=False),
    Column("algorithm_sources_json", JSON_DOCUMENT, nullable=False),
    Column("harness_sha256", String(64), nullable=False),
    Column("image_sha256", String(64), nullable=False),
    Column("wall_limit_seconds", Integer, nullable=False),
    Column("deadline_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("suite_id", "suite_version", name="uq_care_calibration_suite_version"),
    CheckConstraint("suite_id = 'care-farm-b-measured'", name="ck_care_calibration_measured_suite"),
    CheckConstraint("suite_version > 0", name="ck_care_calibration_suite_version"),
    CheckConstraint("wall_limit_seconds between 1 and 14400", name="ck_care_calibration_wall"),
    CheckConstraint("epsilon >= 0 and epsilon < 1", name="ck_care_calibration_epsilon"),
    CheckConstraint(
        "task_weight_policy = 'farm-b-equal-task-weight-1-over-15.v1'",
        name="ck_care_calibration_weight_policy",
    ),
    CheckConstraint(
        "task_weight_policy_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_policy_sha"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "suite_manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_suite_sha"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "calibration_manifest_sha256 ~ '^[0-9a-f]{64}$'",
        name="ck_care_calibration_manifest_sha",
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "development_manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_dev_sha"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "source_manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_source_sha"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "source_archive_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_archive_sha"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "harness_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_harness_sha"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint("image_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_image_sha").ddl_if(
        dialect="postgresql"
    ),
    schema="scorer",
)

care_baseline_calibration_control = Table(
    "care_baseline_calibration_control",
    metadata,
    Column("calibration_id", Uuid(as_uuid=True), primary_key=True),
    Column("state", String(16), nullable=False, server_default="ready"),
    Column("reserved_wall_seconds", Integer, nullable=False, server_default="0"),
    Column("terminal_failure_code", String(32)),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["calibration_id"], ["scorer.care_baseline_calibration_jobs.calibration_id"]
    ),
    CheckConstraint(
        "state in ('ready','running','complete','incomplete')",
        name="ck_care_calibration_control_state",
    ),
    CheckConstraint("reserved_wall_seconds >= 0", name="ck_care_calibration_reserved_seconds"),
    schema="scorer",
)

care_baseline_calibration_tasks = Table(
    "care_baseline_calibration_tasks",
    metadata,
    Column("calibration_id", Uuid(as_uuid=True), primary_key=True),
    Column("task_key", String(32), primary_key=True),
    Column("task_id", String(128), nullable=False),
    Column("dataset_id", String(128), nullable=False),
    Column("split_id", String(128), nullable=False),
    Column("session_id", String(256), nullable=False),
    Column("profile_sha256", String(64), nullable=False),
    Column("family", String(8), nullable=False),
    Column("task_weight", Float, nullable=False),
    Column("train_sha256", String(64), nullable=False),
    Column("evaluation_sha256", String(64), nullable=False),
    Column("evaluation_rows", Integer, nullable=False),
    Column("labels_sha256", String(64), nullable=False),
    Column("semantics_sha256", String(64), nullable=False),
    Column("source_member_sha256", String(64), nullable=False),
    Column("sliding_window", Integer, nullable=False),
    Column("sampling_s", Integer, nullable=False),
    Column("context_json", JSON_DOCUMENT, nullable=False),
    ForeignKeyConstraint(
        ["calibration_id"], ["scorer.care_baseline_calibration_jobs.calibration_id"]
    ),
    ForeignKeyConstraint(
        ["dataset_id", "split_id", "session_id"],
        [
            "scorer.dataset_profiles.dataset_id",
            "scorer.dataset_profiles.split_id",
            "scorer.dataset_profiles.session_id",
        ],
    ),
    UniqueConstraint("calibration_id", "dataset_id", "split_id", "session_id"),
    CheckConstraint("task_key ~ '^[0-9a-f]{32}$'", name="ck_care_calibration_task_key").ddl_if(
        dialect="postgresql"
    ),
    CheckConstraint(
        "profile_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_profile_sha"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint("train_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_train_sha").ddl_if(
        dialect="postgresql"
    ),
    CheckConstraint(
        "evaluation_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_eval_sha"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "labels_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_labels_sha"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "semantics_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_semantics_sha"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "source_member_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_member_sha"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint("family in ('PDM','NRM')", name="ck_care_calibration_family"),
    CheckConstraint(
        "abs(task_weight - 0.06666666666666667) < 1e-12", name="ck_care_calibration_weight"
    ),
    CheckConstraint(
        "sliding_window > 0 and sampling_s > 0 and evaluation_rows > 0",
        name="ck_care_calibration_task_shape",
    ),
    schema="scorer",
)

care_baseline_calibration_claims = Table(
    "care_baseline_calibration_claims",
    metadata,
    Column("claim_id", Uuid(as_uuid=True), primary_key=True),
    Column("calibration_id", Uuid(as_uuid=True), nullable=False),
    Column("task_key", String(32), nullable=False),
    Column("algorithm", String(32), nullable=False),
    Column("seed", Integer, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("state", String(16), nullable=False),
    Column("reserved_seconds", Integer, nullable=False),
    Column("reserved_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("deadline_at", DateTime(timezone=True), nullable=False),
    Column("worker_pid", Integer),
    Column("worker_start_ticks", String(32)),
    Column("worker_boot_id", String(36)),
    Column("worker_unit", String(128)),
    Column("worker_invocation_id", String(32)),
    Column("worker_cgroup", String(512)),
    Column("failure_code", String(32)),
    Column("bound_at", DateTime(timezone=True)),
    Column("terminal_at", DateTime(timezone=True)),
    ForeignKeyConstraint(
        ["calibration_id", "task_key"],
        [
            "scorer.care_baseline_calibration_tasks.calibration_id",
            "scorer.care_baseline_calibration_tasks.task_key",
        ],
    ),
    UniqueConstraint(
        "calibration_id", "task_key", "algorithm", "seed", name="uq_care_calibration_cell_claim"
    ),
    CheckConstraint("generation > 0", name="ck_care_calibration_claim_generation"),
    CheckConstraint(
        "state in ('reserved','running','succeeded','failed')",
        name="ck_care_calibration_claim_state",
    ),
    CheckConstraint("reserved_seconds = 100", name="ck_care_calibration_claim_reserved_seconds"),
    CheckConstraint(
        "algorithm in ('robust_z','iforest','ecod_train_frozen')",
        name="ck_care_calibration_claim_algorithm",
    ),
    CheckConstraint("seed in (0,1,2)", name="ck_care_calibration_claim_seed"),
    CheckConstraint(
        "(state='reserved' and worker_pid is null and worker_unit is null) or "
        "(state<>'reserved' and worker_pid is not null and worker_unit is not null) or "
        "(state='failed' and worker_pid is null and worker_unit is null)",
        name="ck_care_calibration_claim_worker_shape",
    ),
    schema="scorer",
)

care_baseline_calibration_cells = Table(
    "care_baseline_calibration_cells",
    metadata,
    Column("calibration_id", Uuid(as_uuid=True), primary_key=True),
    Column("task_key", String(32), primary_key=True),
    Column("algorithm", String(32), primary_key=True),
    Column("seed", Integer, primary_key=True),
    Column("claim_id", Uuid(as_uuid=True), nullable=True),
    Column("claim_generation", Integer, nullable=True),
    Column("baseline_source_sha256", String(64), nullable=False),
    Column("harness_sha256", String(64), nullable=False),
    Column("image_sha256", String(64), nullable=False),
    Column("profile_sha256", String(64), nullable=False),
    Column("train_sha256", String(64), nullable=False),
    Column("evaluation_sha256", String(64), nullable=False),
    Column("labels_sha256", String(64), nullable=False),
    Column("semantics_sha256", String(64), nullable=False),
    Column("fit_context_sha256", String(64), nullable=False),
    Column("fit_artifact_sha256", String(64), nullable=False),
    Column("score_document_sha256", String(64), nullable=False),
    Column("policy_json", JSON_DOCUMENT, nullable=False),
    Column("raw_task_score", Float, nullable=False),
    Column("auxiliary_metrics_json", JSON_DOCUMENT, nullable=False),
    Column("fit_seconds", Float, nullable=False),
    Column("score_seconds", Float, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["calibration_id", "task_key"],
        [
            "scorer.care_baseline_calibration_tasks.calibration_id",
            "scorer.care_baseline_calibration_tasks.task_key",
        ],
    ),
    ForeignKeyConstraint(["claim_id"], ["scorer.care_baseline_calibration_claims.claim_id"]),
    CheckConstraint("claim_generation > 0", name="ck_care_calibration_cell_claim_generation"),
    CheckConstraint(
        "algorithm in ('robust_z','iforest','ecod_train_frozen')",
        name="ck_care_calibration_algorithm",
    ),
    CheckConstraint("seed in (0,1,2)", name="ck_care_calibration_seed"),
    CheckConstraint(
        "raw_task_score >= 0 and raw_task_score <= 1", name="ck_care_calibration_score"
    ),
    CheckConstraint("fit_seconds >= 0 and score_seconds >= 0", name="ck_care_calibration_elapsed"),
    schema="scorer",
)

care_baseline_calibration_task_summaries = Table(
    "care_baseline_calibration_task_summaries",
    metadata,
    Column("calibration_id", Uuid(as_uuid=True), primary_key=True),
    Column("task_key", String(32), primary_key=True),
    Column("base_score", Float, nullable=False),
    Column("reference_score", Float, nullable=False),
    Column("task_weight", Float, nullable=False),
    ForeignKeyConstraint(
        ["calibration_id", "task_key"],
        [
            "scorer.care_baseline_calibration_tasks.calibration_id",
            "scorer.care_baseline_calibration_tasks.task_key",
        ],
    ),
    CheckConstraint(
        "base_score >= 0 and base_score <= reference_score and reference_score <= 1",
        name="ck_care_calibration_summary_scores",
    ),
    CheckConstraint(
        "abs(task_weight - 0.06666666666666667) < 1e-12", name="ck_care_calibration_summary_weight"
    ),
    schema="scorer",
)

care_baseline_calibration_freezes = Table(
    "care_baseline_calibration_freezes",
    metadata,
    Column("calibration_id", Uuid(as_uuid=True), primary_key=True),
    Column("manifest_sha256", String(64), nullable=False),
    Column("calibration_sha256", String(64), nullable=False, unique=True),
    Column("task_count", Integer, nullable=False),
    Column("cell_count", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["calibration_id"], ["scorer.care_baseline_calibration_jobs.calibration_id"]
    ),
    CheckConstraint("task_count = 15", name="ck_care_calibration_freeze_tasks"),
    CheckConstraint("cell_count = 135", name="ck_care_calibration_freeze_cells"),
    CheckConstraint(
        "manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_freeze_manifest_sha"
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "calibration_sha256 ~ '^[0-9a-f]{64}$'", name="ck_care_calibration_freeze_sha"
    ).ddl_if(dialect="postgresql"),
    schema="scorer",
)

holdout_suite_versions = Table(
    "holdout_suite_versions",
    metadata,
    Column("suite_id", String(128), primary_key=True),
    Column("suite_version", Integer, primary_key=True),
    Column("manifest_sha256", String(64), nullable=False),
    Column("development_manifest_sha256", String(64), nullable=False),
    Column("task_count", Integer, nullable=False),
    Column("epsilon", Float, nullable=False),
    Column("care_calibration_id", Uuid(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["care_calibration_id"],
        ["scorer.care_baseline_calibration_freezes.calibration_id"],
        name="fk_holdout_suite_care_calibration",
        ondelete="RESTRICT",
    ),
    CheckConstraint("suite_version > 0", name="ck_holdout_version_positive"),
    CheckConstraint("task_count > 0 and task_count <= 256", name="ck_holdout_task_count"),
    CheckConstraint("epsilon >= 0 and epsilon < 1", name="ck_holdout_epsilon"),
    CheckConstraint(
        "length(manifest_sha256) = 64 and manifest_sha256 = lower(manifest_sha256)",
        name="ck_holdout_manifest_sha",
    ),
    CheckConstraint(
        "length(development_manifest_sha256) = 64 and "
        "development_manifest_sha256 = lower(development_manifest_sha256)",
        name="ck_holdout_development_manifest_sha",
    ),
    schema="scorer",
)

holdout_run_end_intents = Table(
    "holdout_run_end_intents",
    metadata,
    Column(
        "run_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("terminal_status", String(32), nullable=False),
    Column("completed_proposals", Integer, nullable=False),
    Column("proposal_limit", Integer, nullable=False),
    Column("candidate_experiment_id", String(128), nullable=False),
    Column("candidate_sha256", String(64), nullable=False),
    Column("state_checkpoint_key", String(160), nullable=False),
    Column("state_checkpoint_sha256", String(64), nullable=False),
    Column("state_checkpoint_sequence", Integer, nullable=False),
    Column("intent_checkpoint_sha256", String(64), nullable=False),
    Column("intent_checkpoint_sequence", Integer, nullable=False),
    Column("wall_seconds", Float, nullable=False),
    Column("elapsed_wall_seconds", Float, nullable=False),
    Column("reserved_wall_seconds", Float, nullable=False),
    Column("model_tokens", Integer, nullable=False),
    Column("reserved_model_tokens", Integer, nullable=False),
    Column("admitted_generation", Integer),
    Column("execution_sha256", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["candidate_experiment_id"], ["lab.experiments.experiment_id"], ondelete="RESTRICT"
    ),
    CheckConstraint(
        "terminal_status in ('proposal_limit_reached','budget_exhausted')",
        name="ck_holdout_run_end_status",
    ),
    CheckConstraint("completed_proposals >= 0", name="ck_holdout_run_end_completed"),
    CheckConstraint("proposal_limit > 0", name="ck_holdout_run_end_limit"),
    CheckConstraint("completed_proposals <= proposal_limit", name="ck_holdout_run_end_count"),
    CheckConstraint(
        "length(candidate_sha256) = 64 and candidate_sha256 = lower(candidate_sha256)",
        name="ck_holdout_run_end_candidate_sha",
    ),
    CheckConstraint(
        "length(state_checkpoint_key) > 0",
        name="ck_holdout_run_end_state_key",
    ),
    CheckConstraint(
        "length(state_checkpoint_sha256) = 64 and "
        "state_checkpoint_sha256 = lower(state_checkpoint_sha256)",
        name="ck_holdout_run_end_state_sha",
    ),
    CheckConstraint(
        "length(intent_checkpoint_sha256) = 64 and "
        "intent_checkpoint_sha256 = lower(intent_checkpoint_sha256)",
        name="ck_holdout_run_end_intent_sha",
    ),
    CheckConstraint(
        "state_checkpoint_sequence >= 0 and intent_checkpoint_sequence > state_checkpoint_sequence",
        name="ck_holdout_run_end_sequences",
    ),
    CheckConstraint(
        "wall_seconds >= 0 and elapsed_wall_seconds >= 0 and reserved_wall_seconds >= 0",
        name="ck_holdout_run_end_wall",
    ),
    CheckConstraint(
        "model_tokens >= 0 and reserved_model_tokens >= 0",
        name="ck_holdout_run_end_tokens",
    ),
    CheckConstraint(
        "(admitted_generation is null and execution_sha256 is null) or "
        "(admitted_generation is not null and admitted_generation > 0 and "
        "execution_sha256 is not null and length(execution_sha256) = 64 and "
        "execution_sha256 = lower(execution_sha256))",
        name="ck_holdout_run_end_owner_pair",
    ),
    ForeignKeyConstraint(
        ["run_id", "admitted_generation", "execution_sha256"],
        [
            "lab.director_owner_generations.run_id",
            "lab.director_owner_generations.generation",
            "lab.director_owner_generations.execution_sha256",
        ],
        name="fk_holdout_run_end_owner_pair",
        ondelete="RESTRICT",
    ),
    schema="lab",
)

# These tables are installed by holdout migrations 0020 and 0021. Keeping them
# in the shared metadata makes isolated metadata builds reflect the live schema.
holdout_run_end_fences = Table(
    "holdout_run_end_fences",
    metadata,
    Column(
        "run_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.holdout_run_end_intents.run_id"),
        primary_key=True,
    ),
    Column(
        "candidate_experiment_id",
        String(128),
        ForeignKey("lab.experiments.experiment_id"),
        nullable=False,
    ),
    Column("intent_checkpoint_sha256", String(64), nullable=False),
    Column("intent_checkpoint_sequence", Integer, nullable=False),
    Column("budget_checkpoint_key", String(160), nullable=False),
    Column("budget_checkpoint_sha256", String(64), nullable=False),
    Column(
        "failure_kind",
        String(48),
        nullable=False,
        server_default="missing_reservation_unverifiable_budget",
    ),
    Column("admitted_generation", Integer),
    Column("execution_sha256", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "intent_checkpoint_sha256 ~ '^[0-9a-f]{64}$'",
        name="ck_holdout_fence_intent_sha",
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "budget_checkpoint_sha256 ~ '^[0-9a-f]{64}$'",
        name="ck_holdout_fence_budget_sha",
    ).ddl_if(dialect="postgresql"),
    CheckConstraint("intent_checkpoint_sequence > 0", name="ck_holdout_fence_sequence"),
    CheckConstraint(
        "failure_kind='missing_reservation_unverifiable_budget'",
        name="ck_holdout_fence_failure_kind",
    ),
    CheckConstraint(
        "(admitted_generation is null and execution_sha256 is null) or "
        "(admitted_generation is not null and admitted_generation > 0 and "
        "execution_sha256 is not null and length(execution_sha256) = 64 and "
        "execution_sha256 = lower(execution_sha256))",
        name="ck_holdout_fence_owner_pair",
    ),
    ForeignKeyConstraint(
        ["run_id", "admitted_generation", "execution_sha256"],
        [
            "lab.director_owner_generations.run_id",
            "lab.director_owner_generations.generation",
            "lab.director_owner_generations.execution_sha256",
        ],
        name="fk_holdout_fence_owner_pair",
        ondelete="RESTRICT",
    ),
    schema="lab",
)

holdout_run_end_unavailable = Table(
    "holdout_run_end_unavailable",
    metadata,
    Column(
        "run_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("candidate_experiment_id", String(128), nullable=False),
    Column("reason", String(64), nullable=False),
    Column("prior_state_key", String(160), nullable=False),
    Column("prior_state_sha256", String(64), nullable=False),
    Column("prior_state_sequence", Integer, nullable=False),
    Column("budget_snapshot_sha256", String(64), nullable=False),
    Column("unavailable_checkpoint_sha256", String(64), nullable=False),
    Column("unavailable_checkpoint_sequence", Integer, nullable=False),
    Column("original_intent_checkpoint_sha256", String(64)),
    Column("original_intent_checkpoint_sequence", Integer),
    Column("reservation_checkpoint_key", String(160)),
    Column("reservation_checkpoint_sha256", String(64)),
    Column("reservation_checkpoint_sequence", Integer),
    Column("admitted_generation", Integer),
    Column("execution_sha256", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "reason in ('fresh_wall_budget_unavailable', 'intent_checkpoint_without_sql_registration', "
        "'reservation_checkpoint_without_sql_intent')",
        name="ck_run_end_unavailable_reason",
    ),
    CheckConstraint(
        "length(prior_state_sha256)=64 and prior_state_sha256 ~ '^[0-9a-f]{64}$'",
        name="ck_run_end_unavailable_state_sha",
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "length(budget_snapshot_sha256)=64 and budget_snapshot_sha256 ~ '^[0-9a-f]{64}$'",
        name="ck_run_end_unavailable_budget_sha",
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "length(unavailable_checkpoint_sha256)=64 and "
        "unavailable_checkpoint_sha256 ~ '^[0-9a-f]{64}$'",
        name="ck_run_end_unavailable_checkpoint_sha",
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "(original_intent_checkpoint_sha256 is null and "
        "original_intent_checkpoint_sequence is null) or "
        "(original_intent_checkpoint_sha256 is not null and "
        "original_intent_checkpoint_sha256 ~ '^[0-9a-f]{64}$' and "
        "original_intent_checkpoint_sequence is not null and "
        "original_intent_checkpoint_sequence >= 0)",
        name="ck_run_end_unavailable_original_intent",
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "(reason='fresh_wall_budget_unavailable' and "
        "original_intent_checkpoint_sha256 is null) or "
        "(reason='intent_checkpoint_without_sql_registration' and "
        "original_intent_checkpoint_sha256 is not null) or "
        "(reason='reservation_checkpoint_without_sql_intent' and "
        "original_intent_checkpoint_sha256 is null)",
        name="ck_run_end_unavailable_reason_intent",
    ),
    CheckConstraint(
        "(reason='reservation_checkpoint_without_sql_intent' and "
        "reservation_checkpoint_key is not null and reservation_checkpoint_sha256 is not null and "
        "reservation_checkpoint_sha256 ~ '^[0-9a-f]{64}$' and "
        "reservation_checkpoint_sequence is not null and reservation_checkpoint_sequence >= 0) or "
        "(reason<>'reservation_checkpoint_without_sql_intent' and "
        "reservation_checkpoint_key is null and reservation_checkpoint_sha256 is null and "
        "reservation_checkpoint_sequence is null)",
        name="ck_run_end_unavailable_reservation_checkpoint",
    ).ddl_if(dialect="postgresql"),
    CheckConstraint(
        "(admitted_generation is null and execution_sha256 is null) or "
        "(admitted_generation is not null and admitted_generation > 0 and "
        "execution_sha256 is not null and length(execution_sha256) = 64 and "
        "execution_sha256 = lower(execution_sha256))",
        name="ck_run_end_unavailable_owner_pair",
    ),
    ForeignKeyConstraint(
        ["run_id", "admitted_generation", "execution_sha256"],
        [
            "lab.director_owner_generations.run_id",
            "lab.director_owner_generations.generation",
            "lab.director_owner_generations.execution_sha256",
        ],
        name="fk_run_end_unavailable_owner_pair",
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "prior_state_sequence >= 0 and unavailable_checkpoint_sequence > prior_state_sequence",
        name="ck_run_end_unavailable_sequences",
    ),
    schema="lab",
)

holdout_suite_tasks = Table(
    "holdout_suite_tasks",
    metadata,
    Column("suite_id", String(128), primary_key=True),
    Column("suite_version", Integer, primary_key=True),
    Column("task_key", String(64), primary_key=True),
    Column("dataset_id", String(128), nullable=False),
    Column("split_id", String(128), nullable=False),
    Column("session_id", String(256), nullable=False),
    Column("profile_sha256", String(64), nullable=False),
    Column("family", String(8), nullable=False),
    Column("task_weight", Float, nullable=False),
    Column("base_score", Float, nullable=False),
    Column("reference_score", Float, nullable=False),
    Column("sliding_window", Integer, nullable=False),
    Column("sampling_s", Integer),
    Column("train_sha256", String(64), nullable=False),
    Column("evaluation_sha256", String(64), nullable=False),
    Column("semantics_sha256", String(64), nullable=False),
    Column("context_json", JSON_DOCUMENT, nullable=False),
    ForeignKeyConstraint(
        ("suite_id", "suite_version"),
        (
            "scorer.holdout_suite_versions.suite_id",
            "scorer.holdout_suite_versions.suite_version",
        ),
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ("dataset_id", "split_id", "session_id"),
        (
            "scorer.dataset_profiles.dataset_id",
            "scorer.dataset_profiles.split_id",
            "scorer.dataset_profiles.session_id",
        ),
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "length(task_key) = 32 and task_key = lower(task_key)", name="ck_holdout_task_key"
    ),
    CheckConstraint(
        "length(profile_sha256) = 64 and profile_sha256 = lower(profile_sha256)",
        name="ck_holdout_profile_sha",
    ),
    CheckConstraint("family in ('EVT','PDM','NRM')", name="ck_holdout_family"),
    CheckConstraint("task_weight > 0", name="ck_holdout_task_weight"),
    CheckConstraint(
        "base_score >= 0 and base_score <= reference_score and reference_score <= 1",
        name="ck_holdout_normalization",
    ),
    CheckConstraint("sliding_window > 0", name="ck_holdout_window"),
    CheckConstraint(
        "(family='EVT' and (sampling_s is null or sampling_s > 0)) or "
        "(family in ('PDM','NRM') and sampling_s is not null and sampling_s > 0)",
        name="ck_holdout_sampling",
    ),
    CheckConstraint(
        "length(train_sha256) = 64 and train_sha256 = lower(train_sha256)",
        name="ck_holdout_train_sha",
    ),
    CheckConstraint(
        "length(evaluation_sha256) = 64 and evaluation_sha256 = lower(evaluation_sha256)",
        name="ck_holdout_eval_sha",
    ),
    CheckConstraint(
        "length(semantics_sha256) = 64 and semantics_sha256 = lower(semantics_sha256)",
        name="ck_holdout_semantics_sha",
    ),
    schema="scorer",
)

holdout_suite_quotas = Table(
    "holdout_suite_quotas",
    metadata,
    Column("suite_id", String(128), primary_key=True),
    Column("suite_version", Integer, primary_key=True),
    Column("used", Integer, nullable=False, server_default="0"),
    ForeignKeyConstraint(
        ("suite_id", "suite_version"),
        (
            "scorer.holdout_suite_versions.suite_id",
            "scorer.holdout_suite_versions.suite_version",
        ),
        ondelete="RESTRICT",
    ),
    CheckConstraint("used >= 0 and used <= 100", name="ck_holdout_suite_quota"),
    schema="lab",
)

holdout_run_quotas = Table(
    "holdout_run_quotas",
    metadata,
    Column(
        "run_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("suite_id", String(128), nullable=False),
    Column("suite_version", Integer, nullable=False),
    Column("manifest_sha256", String(64), nullable=False),
    Column("used", Integer, nullable=False, server_default="0"),
    ForeignKeyConstraint(
        ("suite_id", "suite_version"),
        (
            "scorer.holdout_suite_versions.suite_id",
            "scorer.holdout_suite_versions.suite_version",
        ),
        ondelete="RESTRICT",
    ),
    CheckConstraint("used >= 0 and used <= 20", name="ck_holdout_run_quota"),
    CheckConstraint(
        "length(manifest_sha256) = 64 and manifest_sha256 = lower(manifest_sha256)",
        name="ck_holdout_run_manifest",
    ),
    schema="lab",
)

holdout_approvals = Table(
    "holdout_approvals",
    metadata,
    Column(
        "run_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("candidate_sha256", String(64), nullable=False),
    Column("reservation_id", Uuid(as_uuid=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ("run_id", "reservation_id"),
        ("lab.holdout_reservations.run_id", "lab.holdout_reservations.reservation_id"),
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "length(candidate_sha256) = 64 and candidate_sha256 = lower(candidate_sha256)",
        name="ck_holdout_approval_sha",
    ),
    schema="lab",
)

holdout_reservations = Table(
    "holdout_reservations",
    metadata,
    Column("reservation_id", Uuid(as_uuid=True), primary_key=True),
    Column(
        "run_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.runs.run_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("request_key", String(128), nullable=False),
    Column("suite_id", String(128), nullable=False),
    Column("suite_version", Integer, nullable=False),
    Column("manifest_sha256", String(64), nullable=False),
    Column("candidate_experiment_id", String(128), nullable=False),
    Column("candidate_sha256", String(64), nullable=False),
    Column("candidate_blob_sha256", String(64), nullable=False),
    Column("reference_sha256", String(64), nullable=False),
    Column("reference_blob_sha256", String(64), nullable=False),
    Column("trigger_kind", String(16), nullable=False),
    Column("trigger_index", Integer, nullable=False),
    Column("epsilon", Float, nullable=False),
    Column("state", String(16), nullable=False, server_default="reserved"),
    Column("worker_pid", Integer),
    Column("worker_start_ticks", BigInteger),
    Column("worker_boot_id", String(36)),
    Column("worker_unit", String(255)),
    Column("worker_invocation_id", String(32)),
    Column("worker_cgroup", Text),
    Column("admitted_generation", Integer),
    Column("execution_sha256", String(64)),
    Column("result_bit", Boolean),
    Column("result_sha256", String(64)),
    Column("error_code", String(48)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True)),
    ForeignKeyConstraint(
        ("suite_id", "suite_version"),
        (
            "scorer.holdout_suite_versions.suite_id",
            "scorer.holdout_suite_versions.suite_version",
        ),
        ondelete="RESTRICT",
    ),
    UniqueConstraint("run_id", "reservation_id", name="uq_holdout_run_reservation"),
    UniqueConstraint("run_id", "request_key", name="uq_holdout_run_request"),
    UniqueConstraint("run_id", "trigger_kind", "trigger_index", name="uq_holdout_run_trigger"),
    CheckConstraint("length(request_key) between 1 and 128", name="ck_holdout_request_key"),
    CheckConstraint(
        "length(manifest_sha256) = 64 and manifest_sha256 = lower(manifest_sha256)",
        name="ck_holdout_reservation_manifest",
    ),
    CheckConstraint(
        "length(candidate_blob_sha256) = 64 and "
        "candidate_blob_sha256 = lower(candidate_blob_sha256)",
        name="ck_holdout_candidate_blob_sha",
    ),
    CheckConstraint(
        "length(reference_sha256) = 64 and reference_sha256 = lower(reference_sha256)",
        name="ck_holdout_reference_sha",
    ),
    CheckConstraint(
        "length(reference_blob_sha256) = 64 and "
        "reference_blob_sha256 = lower(reference_blob_sha256)",
        name="ck_holdout_reference_blob_sha",
    ),
    CheckConstraint("trigger_kind in ('keep_interval','run_end')", name="ck_holdout_trigger"),
    CheckConstraint("trigger_index > 0", name="ck_holdout_trigger_index"),
    CheckConstraint(
        "(admitted_generation is null and execution_sha256 is null) or "
        "(admitted_generation is not null and admitted_generation > 0 and "
        "execution_sha256 is not null and length(execution_sha256) = 64 and "
        "execution_sha256 = lower(execution_sha256))",
        name="ck_holdout_reservation_owner_pair",
    ),
    ForeignKeyConstraint(
        ["run_id", "admitted_generation", "execution_sha256"],
        [
            "lab.director_owner_generations.run_id",
            "lab.director_owner_generations.generation",
            "lab.director_owner_generations.execution_sha256",
        ],
        name="fk_holdout_reservation_owner_pair",
        ondelete="RESTRICT",
    ),
    CheckConstraint("epsilon >= 0 and epsilon < 1", name="ck_holdout_reservation_epsilon"),
    CheckConstraint(
        "state in ('reserved','running','passed','reverted','failed','exhausted')",
        name="ck_holdout_reservation_state",
    ),
    CheckConstraint(
        "(worker_pid is null and worker_start_ticks is null and worker_boot_id is null and "
        "worker_unit is null and worker_invocation_id is null and worker_cgroup is null) or "
        "(worker_pid is not null and worker_start_ticks is not null and worker_boot_id is not null "
        "and worker_unit is not null and worker_invocation_id is not null "
        "and worker_cgroup is not null "
        "and worker_pid > 1 and worker_start_ticks > 0 and length(worker_boot_id) = 36 and "
        "length(worker_unit) > 0 and length(worker_invocation_id) = 32 and "
        "length(worker_cgroup) > 0)",
        name="ck_holdout_worker_identity",
    ),
    CheckConstraint(
        "(state in ('passed','reverted') and result_bit is not null and result_sha256 is not null "
        "and error_code is null) or "
        "(state in ('reserved','running','exhausted') and result_bit is null "
        "and result_sha256 is null and error_code is null) or "
        "(state='failed' and result_bit is null "
        "and result_sha256 is null and error_code is not null)",
        name="ck_holdout_result_shape",
    ),
    schema="lab",
)

holdout_results = Table(
    "holdout_results",
    metadata,
    Column(
        "reservation_id",
        Uuid(as_uuid=True),
        ForeignKey("lab.holdout_reservations.reservation_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("result_json", JSON_DOCUMENT, nullable=False),
    Column("result_sha256", String(64), nullable=False),
    Column("delta", Float, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "length(result_sha256) = 64 and result_sha256 = lower(result_sha256)",
        name="ck_holdout_result_sha",
    ),
    schema="scorer",
)


terminal_finalizations = Table(
    "terminal_finalizations",
    metadata,
    Column("run_id", Uuid(as_uuid=True), ForeignKey("lab.runs.run_id"), primary_key=True),
    Column("generation", Integer, nullable=False),
    Column("invocation_id", Text, nullable=False),
    Column("execution_sha256", Text, nullable=False),
    Column("payload_sha256", Text, nullable=False),
    Column("source_kind", Text, nullable=False),
    Column("source_sha256", Text, nullable=False),
    Column("task_plan_sha256", Text, nullable=False),
    Column("task_plan_count", Integer, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("deadline_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("generation > 0"),
    CheckConstraint("invocation_id ~ '^[0-9a-f]{32}$'").ddl_if(dialect="postgresql"),
    CheckConstraint("execution_sha256 ~ '^[0-9a-f]{64}$'").ddl_if(dialect="postgresql"),
    CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'").ddl_if(dialect="postgresql"),
    CheckConstraint("source_kind IN ('holdout','baseline')"),
    CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'").ddl_if(dialect="postgresql"),
    CheckConstraint("task_plan_sha256 ~ '^[0-9a-f]{64}$'").ddl_if(dialect="postgresql"),
    CheckConstraint("task_plan_count > 0"),
    schema="lab",
)
