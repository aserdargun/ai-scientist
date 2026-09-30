export type AcceptanceStatus = 'passed' | 'partial' | 'open';
export interface EvidenceRef { name: string; url: string; kind: 'document' | 'json' | 'html' | 'text' }
export interface AcceptanceItem {
  id: string;
  title: string;
  status: AcceptanceStatus;
  detail: string;
  evidence: EvidenceRef[];
}
export interface Overview {
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
  purpose?: 'baseline' | 'research' | 'mode-grid';
  run_id: string; state: string; origin?: string; created_at?: string; updated_at?: string;
  stop_requested?: boolean; report_sha256?: string | null; stale?: boolean; unavailable?: boolean;
}
export interface Runs { items: Run[] }
export interface StartRun {
  idempotency_key: string; track: 'anomaly' | 'mode'; suite: string;
  budget: { experiments: number; wall_seconds: number; model_tokens: number }; program_version: string;
}
export interface EvidenceResponse { name: string; kind: string; content: string; sha256: string; truncated: boolean }

export interface StartBaseline {
  idempotency_key: string; suite: string; program_version: string;
  budget: { experiments: 0; wall_seconds: number; model_tokens: 0 };
}
