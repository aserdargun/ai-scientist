export type AcceptanceStatus = 'passed' | 'partial' | 'open';
export interface EvidenceRef { name: string; url: string; kind: 'document' | 'json' | 'html' | 'text' }
export interface AcceptanceItem {
  id: string;
  title: string;
  status: AcceptanceStatus;
  detail: string;
  evidence: EvidenceRef[];
}
export interface DevelopmentProgress {
  schema: 'development-progress.v1'; updated_at: string;
  current_work: string; latest_result: string; next_step: string;
  cpu_status: 'pending' | 'running' | 'passed' | 'blocked' | 'quarantined';
  gpu_status: 'pending' | 'running' | 'passed' | 'blocked' | 'quarantined';
  research?: {
    run_id: string; state: string; phase: string;
    baseline_completed: number; baseline_target: number;
    model_proposals: number | null; model_proposal_limit: number;
  };
}
export interface Overview {
  development_progress?: DevelopmentProgress | null;
  version: string;
  generated_at: string;
  acceptance: {
    total: number; passed: number; partial: number; open: number;
    source: string; source_sha256: string; items: AcceptanceItem[];
  };
  system: {
    memory: { total_bytes: number; available_bytes: number };
    disk: { total_bytes: number; free_bytes: number };
    cpu: { logical_count: number; load_1m: number | null };
    gpu: {
      available: boolean; name: string | null; total_mib: number | null; used_mib: number | null;
      utilization_percent: number | null; temperature_c: number | null; reason: string | null;
    };
  };
  lab: {
    configured: boolean; connected: boolean; reason: string | null; model_runs_enabled: boolean;
    suites: Suite[];
  };
}
export interface Suite {
  suite_id: string; track: 'anomaly' | 'mode'; program_version: string; provider: string; proposal_limit: number;
  max_experiments?: number; max_wall_seconds?: number; max_model_tokens?: number;
}
export interface Check {
  id: string; state: 'queued' | 'running' | 'passed' | 'failed'; started_at: string | null;
  finished_at: string | null; exit_code: number | null; summary: string | null; output: string | null;
}
export interface Checks { items: Check[] }
export interface Run {
  purpose?: 'baseline' | 'research' | 'mode-grid' | 'mode-stream';
  run_id: string; state: string; origin?: string; created_at?: string; updated_at?: string;
  stop_requested?: boolean; report_sha256?: string | null; stale?: boolean; unavailable?: boolean;
}
export interface Runs { items: Run[] }
export interface FieldIntent {
  asset_id: string; goal_kind: 'digital_twin' | 'predictive_maintenance'; objective: string;
}
export interface FieldContextUsage {
  field_context_sha256: string; snapshot_sha256: string; intent: FieldIntent;
  asset_identity: 'user_supplied'; use: 'advisory-only';
  status: 'admitted-only' | 'context-bound'; context_bound_proposal_count: number;
}
export interface PriorExperience {
  source_run_id: string; source_report_sha256: string;
  records: { experiment_id: string; experiment_sha256: string; trajectory_sha256: string }[];
}
export interface PriorFindingsUsage {
  snapshot_sha256: string; source_run_id: string; source_report_sha256: string;
  record_count: number; context_bound_proposal_count: number;
  status: 'admitted-only' | 'context-bound'; scope: 'historical-advisory-only';
}
export interface ExperienceRecord {
  record_id: string; sequence: number; kind: string; status: string;
  experiment_id?: string | null; method?: string | null; move?: string | null;
  decision?: string | null; reason?: string | null; score?: number | null;
  score_kind?: 'dev-suite' | 'withheld' | 'not-scored';
  model_id?: string | null; model_receipt_status: string;
  experiment_sha256?: string | null; trajectory_sha256?: string | null;
  provenance_summary: { source_count: number; manifest_sha256s: string[]; usage_profile: string };
  training_eligibility: { eligible: false; reasons: string[] };
  history_eligibility?: { eligible: boolean; reasons: string[] };
}
export interface RunExperience {
  schema: 'run-experience.v1'; run_id: string; report_sha256: string; purpose: string;
  verification: 'report-and-ledger-hash-verified'; record_limit: number;
  records: ExperienceRecord[];
  feedback: { same_run_recent_limit: number; cross_run_reuse: false };
  training_started: false; holdout_included: false;
  protected_records_excluded?: boolean;
  prior_findings_usage?: PriorFindingsUsage;
  field_context_usage?: FieldContextUsage;
}
export interface StartRun {
  idempotency_key: string; track: 'anomaly' | 'mode'; suite: string;
  budget: { experiments: number; wall_seconds: number; model_tokens: number }; program_version: string;
}
export interface EvidenceResponse { name: string; kind: string; content: string; sha256: string; truncated: boolean }

export interface StartBaseline {
  idempotency_key: string; suite: string; program_version: string;
  budget: { experiments: 0; wall_seconds: number; model_tokens: 0 };
}

export type StreamSource = { source_id: string; source_version: string; entity: string | null; units: string[]; first_utc: string; train_end_utc: string; last_utc: string; sampling_seconds: number | null; timeline_sha256: string; source_definition_sha256: string; gap_count: number | null; alarm_basis: 'observed_rows'; local_export_allowed: boolean };
export type StreamCatalogSource = { source_id: string; sensors: string[]; source_version: string; entity_required: boolean; row_limit: number; stream: { row_limit: number; capture_seconds: number; local_export_allowed: boolean } | null };
export type StreamInput = { input_sha256: string; source_kind: string; sensors: string[]; train_rows: number; evaluation_rows: number; chunk_count: number; scoring_available: false; source?: StreamSource | null };
export type StreamProgress = { run_id: string; state: string; stop_requested: boolean; input_sha256: string; model_sha256: string | null; fit_artifact_sha256: string | null; total_rows: number; committed_rows: number; committed_chunks: number; latest_chunk_index: number | null; finished: boolean; scoring_available: false; failure_reason: string | null; program_version: 'mode-stream.v1'; source_kind: string; configuration: Record<string, string | number>; sensors: string[]; alarm_threshold: number | null; source?: StreamSource | null };
export type StreamRow = { index: number; state: string; mode_id: number | null; reference_mode_id: number | null; mode_distance: number | null; mode_tolerance: number | null; omr_percent: number | null; reason: string | null; alarm: boolean | null; som_bmu: number | null; som_distance: number | null; residuals: { sensor: string; actual: number | null; predicted: number | null; signed_difference: number | null; absolute_difference: number | null; training_range: number; relative_deviation: number | null; contribution: number | null; excluded_reason: string | null; constant_changed: boolean }[] };
export type StreamChunk = { run_id: string; chunk_index: number; row_offset: number; row_count: number; input_sha256: string; model_sha256: string; fit_artifact_sha256: string; previous_chunk_sha256: string | null; artifact_sha256: string; candidate_derived: true; scoring_available: false; timestamps_utc?: string[]; gaps_before?: boolean[]; prediction: { model_sha256: string; rows: StreamRow[]; alarm_state: { active: boolean; pending: number } } };
