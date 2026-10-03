import { useEffect, useState } from 'react';
import { request } from './api';
import type { StreamChunk, StreamProgress } from './types';
const fmt = (n: number | null | undefined) => n == null ? '—' : n.toLocaleString('tr-TR', { maximumFractionDigits: 4 });
const terminal = new Set(['completed', 'stopped', 'failed']);
export function ModeStreamProgress({ runId }: { runId: string }) {
  const [progress, setProgress] = useState<StreamProgress | null>(null);
  const [index, setIndex] = useState(0);
  const [chunk, setChunk] = useState<StreamChunk | null>(null);
  const [rowIndex, setRowIndex] = useState(0);
  const [sensorName, setSensorName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [chunkError, setChunkError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); let timer = 0;
    let failures = 0;
    async function poll() {
      let finished = false;
      try {
        const next = await request<StreamProgress>(`/runs/${encodeURIComponent(runId)}/mode-stream`, {}, controller.signal);
        if (controller.signal.aborted) return;
        setProgress(next); setError(null); failures = 0; finished = terminal.has(next.state);
      } catch (cause) {
        if (controller.signal.aborted) return;
        failures++; setError(cause instanceof Error ? cause.message : 'Akış durumu yüklenemedi');
      }
      if (!controller.signal.aborted && !finished && failures < 5) timer = window.setTimeout(() => void poll(), Math.min(2500 * 2 ** failures, 30000));
    }
    void poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [runId, retry]);
  // Committed chunks are immutable. Poll only metadata; load one selected page once.
  const available = !!progress && index < progress.committed_chunks;
  useEffect(() => {
    setChunk(null); setChunkError(null); setRowIndex(0);
    if (!available) return;
    const controller = new AbortController();
    request<StreamChunk>(`/runs/${encodeURIComponent(runId)}/mode-stream/chunks/${index}`, {}, controller.signal)
      .then(value => { if (!controller.signal.aborted) setChunk(value); })
      .catch(cause => { if (!controller.signal.aborted) setChunkError(cause instanceof Error ? cause.message : 'Parça yüklenemedi'); });
    return () => controller.abort();
  }, [runId, index, available, retry]);
  const row = chunk?.prediction.rows[rowIndex];
  const rows = chunk?.prediction.rows ?? [];
  const times = chunk?.timestamps_utc?.map(value => Date.parse(value)) ?? [];
  const timed = times.length === rows.length && times.every(Number.isFinite);
  const firstTime = times[0] ?? 0;
  const span = (times[times.length - 1] ?? firstTime) - firstTime;
  const xPosition = (position: number) => 5 + 590 * (timed ? (times[position] - firstTime) / Math.max(span, 1) : position / Math.max(rows.length - 1, 1));
  const source = progress?.source;
  const canDownload = !!progress && (progress.source_kind === 'synthetic' || source?.local_export_allowed === true);
  const maximum = Math.max(1, ...rows.map(item => item.omr_percent ?? 0));
  const sensors = rows[0]?.residuals.map(item => item.sensor) ?? [];
  const selectedSensor = sensors.includes(sensorName) ? sensorName : sensors[0];
  const sensorSeries = rows.map(item => item.residuals.find(sensor => sensor.sensor === selectedSensor));
  const sensorValues = sensorSeries.flatMap(sensor => [sensor?.actual, sensor?.predicted]).filter((n): n is number => n != null);
  const sensorMinimum = sensorValues.length ? Math.min(...sensorValues) : 0;
  const sensorMaximum = sensorValues.length ? Math.max(...sensorValues) : 1;
  function sensorPath(field: 'actual' | 'predicted') {
    let path = ''; let connected = false;
    sensorSeries.forEach((sensor, position) => {
      const value = sensor?.[field];
      if (value == null) { connected = false; return; }
      if (chunk?.gaps_before?.[position]) connected = false;
      const x = xPosition(position);
      const y = 120 - 110 * (value - sensorMinimum) / Math.max(sensorMaximum - sensorMinimum, 1e-12);
      path += `${connected ? ' L' : ' M'}${x.toFixed(2)},${y.toFixed(2)}`; connected = true;
    });
    return path;
  }
  function download() {
    if (!chunk || !canDownload) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(chunk, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a'); link.href = url; link.download = `mode-stream-${runId}-chunk-${index}.json`; link.click(); URL.revokeObjectURL(url);
  }
  return <section><h3>Canlı OMR ve sensör ayrıntıları</h3><p>CPU tanı akışı · puanlanmaz. OMR yüzdesi olasılık değildir. Mod uzaklığı ve SOM uzaklığı ayrı ölçümlerdir.</p>
    {error && <p role="alert">{error}</p>}{(error || chunkError) && <button className="button secondary" onClick={() => setRetry(value => value + 1)}>Yeniden dene</button>}
    {progress && <><p role="status">{error ? 'Son doğrulanmış durum: ' : ''}{progress.state} · {progress.committed_rows}/{progress.total_rows} satır · {progress.committed_chunks} tamamlanmış parça</p><progress aria-label="Kalıcı satır ilerlemesi" value={progress.committed_rows} max={Math.max(1, progress.total_rows)}/>
      {progress.stop_requested && !terminal.has(progress.state) && <p>Durdurma isteği alındı; koşunun sona ermesi bekleniyor.</p>}{progress.failure_reason && <p role="alert">{progress.failure_reason}</p>}
      <p>{String(progress.configuration.method).toUpperCase()} · k={progress.configuration.k} · alarm eşiği {fmt(progress.alarm_threshold)}</p>
      <details><summary>Kaynak ve model kimlikleri</summary><p>Girdi <code>{progress.input_sha256}</code></p><p>Model <code>{progress.model_sha256 ?? 'Fit bekleniyor'}</code></p><p>Fit artefaktı <code>{progress.fit_artifact_sha256 ?? 'Bekleniyor'}</code></p></details>
      {source && <><p>Kaynak {source.source_id} · sürüm {source.source_version} · varlık {source.entity ?? '—'} · {source.first_utc} — {source.last_utc} · eğitim sonu {source.train_end_utc}</p><p>{source.sampling_seconds == null ? 'Örnekleme aralığı bilinmiyor; zaman boşluğu sayısı hesaplanmaz.' : `Beklenen örnekleme ${source.sampling_seconds} saniye · ${source.gap_count ?? '—'} zaman boşluğu.`} Alarmın bekleme sayacı gözlenen satırları sayar.</p><details><summary>Veri kökeni</summary><p>Zaman çizgisi <code>{source.timeline_sha256}</code></p><p>Kaynak tanımı <code>{source.source_definition_sha256}</code></p><p>Sensör birimleri: {source.units.length ? source.units.join(', ') : 'Belirtilmedi'}</p><p>Güvenilir etiket ve Scorer ölçümü yok. Yerel indirme ayarı yayımlama veya eğitim izni vermez.</p></details></>}
      {progress.committed_chunks > 0 ? <><label>Kalıcı parça (0’dan başlar)<input type="number" min="0" max={progress.committed_chunks - 1} value={index} onChange={e => { const next = Number(e.target.value); if (Number.isInteger(next) && next >= 0 && next < progress.committed_chunks) setIndex(next); }}/></label><button className="button secondary" disabled={index === 0} onClick={() => setIndex(value => value - 1)}>Önceki parça</button><button className="button secondary" disabled={index >= progress.committed_chunks - 1} onClick={() => setIndex(value => value + 1)}>Sonraki parça</button><button className="button secondary" onClick={() => setIndex(progress.committed_chunks - 1)}>Son kalıcı parça</button></> : <p>İlk kalıcı parça bekleniyor.</p>}
    </>}{chunkError && <p role="alert">{chunkError}</p>}
    {available && !chunk && !chunkError && <p role="status">Seçilen parça yükleniyor…</p>}
    {chunk && <><p>Parça {chunk.chunk_index} · satır başlangıcı {chunk.row_offset} · {chunk.row_count} satır</p><button className="button secondary" disabled={!canDownload} onClick={download}>Bu parçayı JSON indir</button>{!canDownload && <p>Bu kaynak için yerel JSON indirme kapalı.</p>}<details><summary>Parça kanıtı</summary><code>{chunk.artifact_sha256}</code><p>Önceki parça <code>{chunk.previous_chunk_sha256 ?? 'İlk parça'}</code></p></details>
      {timed ? <p>UTC zaman ekseni: {chunk.timestamps_utc?.[0]} — {chunk.timestamps_utc?.[rows.length - 1]}. Kesikli işaretler beklenen aralığı aşan veri boşluklarıdır.</p> : <p>Bu geçmiş parçanın UTC zamanları yok; eksen satır sırasını gösterir.</p>}
      <svg viewBox="0 0 600 130" width="100%" role="img" aria-label="Seçilen parçanın OMR zaman dizisi">{rows.map((item, position) => <g key={item.index}>{chunk.gaps_before?.[position] && <line x1={xPosition(position)} x2={xPosition(position)} y1="5" y2="125" stroke="#888" strokeDasharray="4 3"><title>{chunk.timestamps_utc?.[position]} öncesinde veri boşluğu</title></line>}{item.omr_percent == null ? null : <circle cx={xPosition(position)} cy={120 - 110 * item.omr_percent / maximum} r="3" fill={item.alarm ? '#e45858' : '#448bd1'}><title>{chunk.timestamps_utc?.[position] ?? `#${item.index}`}: {fmt(item.omr_percent)}% · {item.state}</title></circle>}</g>)}</svg><p>Kırmızı noktalar aday alarmıdır. Görünüm yalnız seçilen parçayı içerir.</p>
      {sensors.length > 0 && <><label>Sensör zaman dizisi<select value={selectedSensor} onChange={e => setSensorName(e.target.value)}>{sensors.map(name => <option key={name}>{name}</option>)}</select></label><p>Mavi: gerçek · turuncu: NN referansı · aralık {fmt(sensorMinimum)}–{fmt(sensorMaximum)}. Eksik değerlerde çizgi kesilir.</p><svg viewBox="0 0 600 130" width="100%" role="img" aria-label={`${selectedSensor} gerçek ve en yakın komşu referansı`}><path d={sensorPath('actual')} fill="none" stroke="#448bd1" strokeWidth="2"/><path d={sensorPath('predicted')} fill="none" stroke="#d8852b" strokeWidth="2"/></svg></>}
      <label>Parça içindeki örnek<input type="range" min="0" max={Math.max(0, rows.length - 1)} value={rowIndex} onChange={e => setRowIndex(Number(e.target.value))}/></label>
      {row && <><p>#{row.index} · UTC {chunk.timestamps_utc?.[rowIndex] ?? 'Geçmiş parçada bulunmuyor'} {chunk.gaps_before?.[rowIndex] ? '· önceki örnekle arasında veri boşluğu' : ''} · {row.state} · mod {row.mode_id ?? '—'} · referans mod {row.reference_mode_id ?? '—'} · OMR {fmt(row.omr_percent)}% · alarm {row.alarm == null ? '—' : row.alarm ? 'Var' : 'Yok'}</p><p>Mod uzaklığı {fmt(row.mode_distance)} / tolerans {fmt(row.mode_tolerance)} · SOM BMU {row.som_bmu ?? '—'} / uzaklık {fmt(row.som_distance)}</p>{row.reason && <p>{row.reason}</p>}
        <div style={{ overflowX: 'auto' }}><table><thead><tr><th>Sensör</th><th>Gerçek</th><th>NN referansı</th><th>İşaretli residual</th><th>Eğitim aralığı</th><th>Göreli sapma</th><th>OMR katkısı</th><th>Durum</th></tr></thead><tbody>{row.residuals.map(sensor => <tr key={sensor.sensor}><td>{sensor.sensor}</td><td>{fmt(sensor.actual)}</td><td>{fmt(sensor.predicted)}</td><td>{fmt(sensor.signed_difference)}</td><td>{fmt(sensor.training_range)}</td><td>{fmt(sensor.relative_deviation)}</td><td>{fmt(sensor.contribution)}</td><td>{sensor.constant_changed ? 'Sabit sensör değişti' : sensor.excluded_reason ?? '—'}</td></tr>)}</tbody></table></div></>}
    </>}
  </section>;
}
