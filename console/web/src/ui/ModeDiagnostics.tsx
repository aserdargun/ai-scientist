import { useState } from 'react';
import { request } from './api';
type Row = { index: number; state: string; mode_id: number | null; omr_percent: number | null; alarm: boolean | null; som_bmu: number | null; som_distance: number | null; residuals: { sensor: string; actual: number | null; predicted: number | null; signed_difference: number | null; contribution: number | null; constant_changed: boolean }[] };
type Diagnostic = { model: { model_sha256: string; config: { method: string; k: number }; modes: { mode_id: number; support: number; tolerance: number }[]; alarm_threshold: number | null }; prediction: { rows: Row[] } };
type Score = { experiment_id: string; task_id: string; seed: number; mode_diagnostics?: { artifact_sha256: string } };
const fmt = (value: number | null) => value == null ? '—' : value.toLocaleString('tr-TR', { maximumFractionDigits: 3 });
export function ModeDiagnostics({ runId, report }: { runId: string; report: unknown }) {
  const [data, setData] = useState<Diagnostic | null>(null);
  const [rowIndex, setRowIndex] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const scores = (report as { report?: { task_scores?: Score[] } })?.report?.task_scores?.filter(score => score.mode_diagnostics) ?? [];
  if (!scores.length) return null;
  async function load(sha: string) { setBusy(true); setError(null); try { const result = await request<{ diagnostics: Diagnostic }>(`/runs/${runId}/mode-diagnostics/${sha}`); setData(result.diagnostics); setRowIndex(0); } catch (cause) { setError(cause instanceof Error ? cause.message : 'Açıklama yüklenemedi'); } finally { setBusy(false); } }
  const row = data?.prediction.rows[rowIndex];
  const values = data?.prediction.rows.map(item => item.omr_percent) ?? [];
  const maximum = Math.max(...values.filter((value): value is number => value !== null), 1);
  return <section><h3>Mod ve sensör açıklamaları</h3><p>Adayın ürettiği açıklamalar; Scorer performans ölçümünden ayrıdır. Kanonik raporun doğruladığı blob okunur; konsolda yeniden fit yapılmaz.</p>
    <label>Aday / tekrar<select disabled={busy} defaultValue="" onChange={e => void load(e.target.value)}><option value="" disabled>Ölçülmüş çıktı seçin</option>{scores.map(score => <option key={score.mode_diagnostics!.artifact_sha256} value={score.mode_diagnostics!.artifact_sha256}>{score.experiment_id} · seed {score.seed} · {score.task_id}</option>)}</select></label>
    {error && <p role="alert">{error}</p>}{data && <><p>{data.model.config.method.toUpperCase()} · k={data.model.config.k} · alarm eşiği {fmt(data.model.alarm_threshold)}</p><code>{data.model.model_sha256}</code><h4>OMR zaman dizisi</h4><svg viewBox="0 0 600 130" width="100%" role="img" aria-label="Ölçülmüş OMR yüzdesi zaman dizisi"><line x1="0" y1="120" x2="600" y2="120" stroke="currentColor"/>{values.map((value, index) => value == null ? null : <circle key={index} cx={index * 590 / Math.max(values.length - 1, 1) + 5} cy={120 - 110 * value / maximum} r="2" fill={data.prediction.rows[index].alarm ? '#e45858' : '#448bd1'}><title>{index}: {fmt(value)}% · {data.prediction.rows[index].state}</title></circle>)}</svg><p>Kırmızı: aday alarmı. OMR yüzde değeri olasılık değildir.</p><table><thead><tr><th>Mod</th><th>Destek</th><th>Tolerans</th></tr></thead><tbody>{data.model.modes.map(mode => <tr key={mode.mode_id}><td>{mode.mode_id}</td><td>{mode.support}</td><td>{fmt(mode.tolerance)}</td></tr>)}</tbody></table><label>Örnek<input type="range" min="0" max={Math.max(data.prediction.rows.length - 1, 0)} value={rowIndex} onChange={e => setRowIndex(Number(e.target.value))}/></label>{row && <><p>#{row.index} · {row.state} · mod {row.mode_id ?? '—'} · OMR {fmt(row.omr_percent)}% · SOM BMU {row.som_bmu ?? '—'} (uzaklık {fmt(row.som_distance)})</p><table><thead><tr><th>Sensör</th><th>Gerçek</th><th>Referans</th><th>Fark</th><th>Katkı</th></tr></thead><tbody>{row.residuals.map(sensor => <tr key={sensor.sensor}><td>{sensor.sensor}{sensor.constant_changed ? ' · sabit sensör değişti' : ''}</td><td>{fmt(sensor.actual)}</td><td>{fmt(sensor.predicted)}</td><td>{fmt(sensor.signed_difference)}</td><td>{fmt(sensor.contribution)}</td></tr>)}</tbody></table></>}</>}
  </section>;
}
