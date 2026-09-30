import type { Check, Checks, EvidenceResponse, Overview, Run, Runs, StartRun, StartBaseline } from './types';

export class ApiError extends Error {
  constructor(message: string, readonly status?: number) { super(message); this.name = 'ApiError'; }
}

export async function request<T>(path: string, init: RequestInit = {}, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/console-api${path}`, {
      ...init, signal: signal ?? init.signal, credentials: 'omit', cache: 'no-store',
      headers: { Accept: 'application/json', ...(init.body ? { 'Content-Type': 'application/json', 'X-Lab-Console': '1' } : {}), ...init.headers },
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError('Yerel konsol API’sine ulaşılamıyor. API sunucusunu başlatıp yeniden deneyin.');
  }
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = typeof data === 'object' && data !== null && 'detail' in data && typeof data.detail === 'string'
      ? data.detail : `İstek başarısız (${response.status}).`;
    throw new ApiError(detail, response.status);
  }
  return data as T;
}
const json = (body: unknown): RequestInit => ({ method: 'POST', body: JSON.stringify(body) });
export const api = {
  overview: (signal?: AbortSignal) => request<Overview>('/overview', {}, signal),
  evidence: (url: string, signal?: AbortSignal) => {
    const prefix = '/console-api/evidence/';
    let parsed: URL;
    try { parsed = new URL(url, window.location.origin); }
    catch { throw new ApiError('Kanıt bağlantısı geçersiz.'); }
    if (parsed.origin !== window.location.origin || parsed.search || parsed.hash || !parsed.pathname.startsWith(prefix)) {
      throw new ApiError('Kanıt bağlantısı geçersiz.');
    }
    let id: string;
    try { id = decodeURIComponent(parsed.pathname.slice(prefix.length)); }
    catch { throw new ApiError('Kanıt bağlantısı geçersiz.'); }
    if (!id || id.includes('/') || id === '.' || id === '..') throw new ApiError('Kanıt bağlantısı geçersiz.');
    return request<EvidenceResponse>(`/evidence/${encodeURIComponent(id)}`, {}, signal);
  },
  checks: (signal?: AbortSignal) => request<Checks>('/checks', {}, signal),
  startCheck: () => request<{ id: string; state: Check['state'] }>('/checks', json({})),
  check: (id: string, signal?: AbortSignal) => request<Check>(`/checks/${encodeURIComponent(id)}`, {}, signal),
  runs: (signal?: AbortSignal) => request<Runs>('/runs', {}, signal),
  watchRun: (runId: string) => request<Run>('/runs/watch', json({ run_id: runId })),
  startBaseline: (body: StartBaseline) => request<{ run_id: string; state: string; reused: boolean }>('/baselines', json(body)),
  startRun: (body: StartRun) => request<{ run_id: string; state: string; reused: boolean }>('/runs', json(body)),
  run: (id: string, signal?: AbortSignal) => request<Run>(`/runs/${encodeURIComponent(id)}`, {}, signal),
  stopRun: (id: string) => request<Run>(`/runs/${encodeURIComponent(id)}/stop`, json({})),
  report: (id: string, signal?: AbortSignal) => request<unknown>(`/runs/${encodeURIComponent(id)}/report`, {}, signal),
};
