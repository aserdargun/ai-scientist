import { t, locale } from './i18n';
import { useEffect, useRef, useState } from 'react';
import { request } from './api';
import { requestId } from './requestId';
import { defaultParameters, ModeParameters, parameterError, scenarioName } from './ModeParameters';
import type { StreamCatalogSource, StreamInput } from './types';

const scenarios = ['healthy_single', 'healthy_multiple', 'healthy_load', 'step', 'drift', 'variance', 'correlation_break', 'oscillation_lag', 'unseen_mode', 'sensor_quality'];
const integer = (n: number, min: number, max: number) => Number.isInteger(n) && n >= min && n <= max;
export function ModeStreamForm({ connected, onStarted }: { connected: boolean; onStarted: () => void }) {
  const [scenario, setScenario] = useState('drift');
  const [seed, setSeed] = useState(0);
  const [rows, setRows] = useState(8192);
  const [sourceKind, setSourceKind] = useState<'synthetic' | 'database'>('synthetic');
  const [sources, setSources] = useState<StreamCatalogSource[]>([]);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [sourceId, setSourceId] = useState('');
  const [sensors, setSensors] = useState<string[]>([]);
  const [startUtc, setStartUtc] = useState('');
  const [endUtc, setEndUtc] = useState('');
  const [entity, setEntity] = useState('');
  const [rowLimit, setRowLimit] = useState(8192);
  const [trainRows, setTrainRows] = useState(192);
  const [method, setMethod] = useState('lsh');
  const [k, setK] = useState(3);
  const [wall, setWall] = useState(1800);
  const [parameters, setParameters] = useState(defaultParameters);
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState<'capture' | 'start' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<string | null>(null);
  const pending = useRef<{ serialized: string; key: string; captureKey: string; input?: StreamInput } | null>(null);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    setSources([]); setSourceError(null);
    if (!connected) return;
    const abort = new AbortController();
    request<{ items: StreamCatalogSource[] }>('/mode-sources', {}, abort.signal)
      .then(result => { if (!abort.signal.aborted) setSources(result.items.filter(source => source.stream != null)); })
      .catch(cause => { if (!abort.signal.aborted) setSourceError(cause instanceof Error ? cause.message : t("Unable to load sources", "Kaynaklar yüklenemedi")); });
    return () => abort.abort();
  }, [connected]);
  const configError = parameterError(parameters);
  const source = sources.find(item => item.source_id === sourceId);
  const utcWindowValid = /(?:Z|\+00:00)$/.test(startUtc) && /(?:Z|\+00:00)$/.test(endUtc) && Number.isFinite(Date.parse(startUtc)) && Date.parse(startUtc) < Date.parse(endUtc);
  const inputValid = sourceKind === 'synthetic' ? integer(rows, 1, 65536) : !!source?.stream && sensors.length > 0 && sensors.length <= 50 && sensors.every(sensor => source.sensors.includes(sensor)) && utcWindowValid && (!source.entity_required || !!entity.trim()) && integer(rowLimit, 17, source.stream.row_limit) && integer(trainRows, 16, Math.min(4096, rowLimit - 1));
  const valid = integer(seed, 0, 4294967295) && inputValid && integer(k, 1, 128) && integer(wall, 1, 14400) && !configError;
  async function start(event: React.FormEvent) {
    event.preventDefault();
    if (busy || !connected || !valid) return;
    setBusy(true); setError(null); setCreated(null);
    const abort = new AbortController(); controller.current = abort;
    const body = sourceKind === 'synthetic' ? { scenario, seed, train_rows: 192, evaluation_rows: rows, chunk_rows: 64 } : { source_id: sourceId, sensors, start_utc: startUtc, end_utc: endUtc, entity: source?.entity_required ? entity : null, row_limit: rowLimit, train_rows: trainRows, chunk_rows: 64 };
    const configuration = { ...parameters, method, seed, k };
    const serialized = JSON.stringify({ sourceKind, body, configuration, wall });
    if (pending.current?.serialized !== serialized) pending.current = { serialized, key: requestId(), captureKey: requestId() };
    const attempt = pending.current!;
    setStage(attempt.input ? 'start' : 'capture');
    try {
      const input = attempt.input ?? await request<StreamInput>(`/mode-stream-inputs/${sourceKind === 'synthetic' ? 'synthetic' : 'database'}`, { method: 'POST', body: JSON.stringify(sourceKind === 'synthetic' ? body : { ...body, idempotency_key: attempt.captureKey }) }, abort.signal);
      attempt.input = input;
      if (abort.signal.aborted) return;
      setStage('start');
      const run = await request<{ run_id: string }>('/mode-streams', { method: 'POST', body: JSON.stringify({ idempotency_key: attempt.key, input_sha256: input.input_sha256, configuration, wall_seconds: wall }) }, abort.signal);
      if (!abort.signal.aborted) { pending.current = null; setCreated(run.run_id); onStarted(); }
    } catch (cause) { if (!abort.signal.aborted) setError(`${cause instanceof Error ? cause.message : t("Unable to start the stream", "Akış başlatılamadı")}. ${t('The launch result could not be verified. Resubmitting the same configuration uses the same request identity; an admitted run may appear in Experiments.', 'Başlatma sonucu doğrulanamadı. Aynı yapılandırmayı tekrar gönderirseniz aynı istek kimliği kullanılır; kabul edilmiş koşu Deneyler listesinde görünebilir.')}`); }
    finally { if (!abort.signal.aborted) { setBusy(false); setStage(null); } }
  }
  function cancelCapture() {
    if (stage !== 'capture') return;
    controller.current?.abort(); setBusy(false); setStage(null);
    setError(t("The capture connection was closed; the server is terminating the query. This response does not verify completed cleanup. Resubmitting the same configuration uses the same capture and launch identities.", "Veri alma bağlantısı kapatıldı; sunucu sorguyu sonlandırıyor. Temizliğin tamamlandığı bu yanıttan doğrulanmaz. Aynı yapılandırmayı tekrar gönderirseniz aynı veri alma ve başlatma kimlikleri kullanılır."));
  }
  return <section className="panel"><h2>{t("Long CPU OMR stream", "Uzun CPU OMR akışı")}</h2>
    <p>{t("Monitor the chronological process in chunks of at most 64 rows. One method is fitted; nearest neighbor references, sensor residuals and OMR are computed continuously. This stream is unscored; it does not measure accuracy or research success.", "Kronolojik süreç en fazla 64 satırlık parçalarla izlenir. Bir yöntem fit edilir; en yakın komşu referansı, sensör residual’ları ve OMR sürekli hesaplanır. Bu akış puanlanmaz; doğruluk veya araştırma başarısı ölçümü değildir.")}</p>
    <form className="run-form" onSubmit={event => void start(event)}><fieldset disabled={busy}><legend>{t("Stream configuration", "Akış yapılandırması")}</legend>
      <label>{t("Input", "Girdi")}<select value={sourceKind} onChange={e => setSourceKind(e.target.value as 'synthetic' | 'database')}><option value="synthetic">{t("Synthetic process", "Sentetik süreç")}</option><option value="database">{t("Authorized PostgreSQL source", "İzin verilen PostgreSQL kaynağı")}</option></select></label>
      {sourceKind === 'synthetic' ? <><label>{t("Scenario", "Senaryo")}<select value={scenario} onChange={e => setScenario(e.target.value)}>{scenarios.map(value => <option key={value} value={value}>{scenarioName(value)}</option>)}</select></label><label>{t("Rows to monitor", "İzlenecek satır")}<input type="number" min="1" max="65536" value={rows} onChange={e => setRows(Number(e.target.value))}/></label><p>{t("The synthetic process uses 192 training rows.", "Sentetik süreçte 192 eğitim satırı kullanılır.")}</p></> : <>
        <p>{t("Connection details stay on the server. Only sources enabled for streaming are listed; actual UTC timestamps and gaps in the selected data are preserved.", "Bağlantı bilgileri sunucuda kalır. Yalnız akış için açılmış kaynaklar listelenir; seçilen verinin gerçek UTC zamanları ve boşlukları korunur.")}</p>
        {sourceError && <p role="alert">{sourceError}</p>}{!sources.length && <p role="status">{t("No authorized PostgreSQL streaming source was found.", "Akış için izin verilen PostgreSQL kaynağı bulunamadı.")}</p>}
        <label>{t("Source", "Kaynak")}<select value={sourceId} onChange={e => { setSourceId(e.target.value); const next = sources.find(item => item.source_id === e.target.value); setSensors(next?.sensors ?? []); const cap = Math.min(8192, next?.stream?.row_limit ?? 8192); setRowLimit(cap); setTrainRows(Math.min(192, cap - 1)); }}><option value="">{t("Select a source", "Kaynak seçin")}</option>{sources.map(item => <option key={item.source_id}>{item.source_id}</option>)}</select></label>
        <fieldset><legend>{t("Sensors (", "Sensörler (")}{sensors.length}/50)</legend>{source?.sensors.map(sensor => <label key={sensor}><input type="checkbox" checked={sensors.includes(sensor)} onChange={e => setSensors(current => e.target.checked ? [...current, sensor] : current.filter(value => value !== sensor))}/>{sensor}</label>)}</fieldset>
        <label>{t("Start (UTC ISO)", "Başlangıç (UTC ISO)")}<input value={startUtc} placeholder="2026-01-01T00:00:00Z" onChange={e => setStartUtc(e.target.value)}/></label><label>{t("End (UTC ISO)", "Bitiş (UTC ISO)")}<input value={endUtc} placeholder="2026-01-02T00:00:00Z" onChange={e => setEndUtc(e.target.value)}/></label>
        {startUtc && endUtc && !utcWindowValid && <p role="alert">{t("Start and end must use UTC with Z or +00:00; end must be after start.", "Başlangıç ve bitiş Z veya +00:00 ile UTC olmalı; bitiş başlangıçtan sonra gelmelidir.")}</p>}
        {source?.entity_required && <label>{t("Asset", "Varlık")}<input maxLength={128} value={entity} onChange={e => setEntity(e.target.value)}/></label>}
        <label>{t("Total row limit (training + monitoring)", "Toplam satır sınırı (eğitim + izleme)")}<input type="number" min="17" max={source?.stream?.row_limit ?? 65536} value={rowLimit} onChange={e => setRowLimit(Number(e.target.value))}/></label><label>{t("Training rows", "Eğitim satırları")}<input type="number" min="16" max={Math.min(4096, rowLimit - 1)} value={trainRows} onChange={e => setTrainRows(Number(e.target.value))}/></label>
        {source?.stream && <p>{t("Source limit ", "Kaynak sınırı ")}{source.stream.row_limit.toLocaleString(locale())}{t(" total rows · capture time at most ", " toplam satır · veri alma süresi en fazla ")}{source.stream.capture_seconds.toLocaleString(locale())}{t(" seconds. Local JSON download is ", " saniye. Yerel JSON indirme ")}{source.stream.local_export_allowed ? t("enabled", "açık") : t("disabled", "kapalı")}{t("; this setting does not grant publication or training permission.", "; bu ayar yayımlama veya eğitim izni vermez.")}</p>}
      </>}
      <label>Seed<input type="number" min="0" max="4294967295" value={seed} onChange={e => setSeed(Number(e.target.value))}/></label>
      <label>{t("Method", "Yöntem")}<select value={method} onChange={e => setMethod(e.target.value)}>{['lsh', 'optics', 'som'].map(value => <option key={value} value={value}>{value.toUpperCase()}</option>)}</select></label>
      <label>{t("Neighbor count k", "Komşu sayısı k")}<input type="number" min="1" max="128" value={k} onChange={e => setK(Number(e.target.value))}/></label>
      <label>{t("Time limit (seconds)", "Süre sınırı (saniye)")}<input type="number" min="1" max="14400" value={wall} onChange={e => setWall(Number(e.target.value))}/></label>
      <ModeParameters methods={[method]} values={parameters} onChange={setParameters}/>
    </fieldset>{configError && <p role="alert">{configError}</p>}
    {stage && <p role="status">{stage === 'capture' ? t("Capturing data…", "Veri alınıyor…") : t("Queuing run…", "Koşu kuyruğa alınıyor…")}</p>}
    {stage === 'capture' && <button type="button" className="button secondary" onClick={cancelCapture}>{t("Cancel data capture", "Veri almayı iptal et")}</button>}
    <button className="button primary" disabled={busy || !connected || !valid}>{busy ? t("Starting…", "Başlatılıyor…") : t("Start CPU stream", "CPU akışını başlat")}</button></form>
    {!connected && <p role="status">{t("A Lab API connection is required.", "Lab API bağlantısı gerekli.")}</p>}{error && <p role="alert" className="field-error">{error}</p>}{created && <p role="status">{t("Run ", "Koşu ")}{created}{t(" was added to monitoring. Open chunks from the experiment details.", " izlemeye eklendi. Deney ayrıntısından parçaları açabilirsiniz.")}</p>}
  </section>;
}
