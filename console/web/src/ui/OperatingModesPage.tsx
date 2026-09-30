import { useEffect, useState } from 'react';
import { request } from './api';
import { defaultParameters, ModeParameters, parameterError } from './ModeParameters';

type Snapshot = { source_kind: string; scoring_available: boolean; snapshot_sha256: string; sensors: string[]; rows: number; train_rows: number; evaluation_rows: number; readiness: string; first_utc: string; last_utc: string };
type Statistic = { value: number | null; reason: string | null };
type Sensor = { name: string; valid: number; missing: number; nonfinite: number; statistics: Record<string, Statistic>; histogram_counts: number[]; histogram_edges: number[]; autocorrelation: Record<string, Statistic>; longest_constant_run: number };
type Summary = { rows: number; sensors: Sensor[]; pairs: { first: string; second: string; pearson: Statistic; spearman: Statistic; valid_pairs: number }[]; timeline: { regular: boolean; gap_count: number; reason: string | null }; notes: string[] };
const scenarios = ['healthy_single', 'healthy_multiple', 'healthy_load', 'step', 'drift', 'variance', 'correlation_break', 'oscillation_lag', 'unseen_mode', 'sensor_quality'];
const post = (body: unknown): RequestInit => ({ method: 'POST', body: JSON.stringify(body) });
const number = (stat?: Statistic) => stat?.value == null ? '—' : stat.value.toLocaleString('tr-TR', { maximumFractionDigits: 4 });

const statisticNames: Record<string, string> = {
  minimum: 'Minimum', maximum: 'Maksimum', mean: 'Ortalama', median: 'Medyan',
  mad: 'Medyan mutlak sapma (MAD)', iqr: 'Çeyrekler arası açıklık (IQR)',
  p01: 'P01', p05: 'P05', p25: 'P25', p75: 'P75', p95: 'P95', p99: 'P99',
  tukey_outlier_count: 'Tukey aykırı değer sayısı', std: 'Standart sapma', variance: 'Varyans',
  skewness: 'Çarpıklık', excess_kurtosis: 'Fazlalık basıklığı', trend_per_second: 'Doğrusal trend / saniye',
};
function StatisticsDetails({ summary }: { summary: Summary }) {
  const [selected, setSelected] = useState('');
  const sensor = summary.sensors.find(item => item.name === selected) ?? summary.sensors[0];
  return <details><summary>Ayrıntılı istatistik ve otokorelasyon</summary>
    <label>Analiz sensörü<select value={sensor?.name ?? ''} onChange={e => setSelected(e.target.value)}>{summary.sensors.map(item => <option key={item.name}>{item.name}</option>)}</select></label>
    {sensor && <><p>Eksik: {sensor.missing} · sonlu olmayan: {sensor.nonfinite} · en uzun sabit dizi: {sensor.longest_constant_run} örnek</p>
      <p>Varyans ve standart sapma örneklem içindir (n−1). Aykırı değerler silinmez; bu işaretler süreç arızası etiketi değildir.</p>
      <table><thead><tr><th>Özellik</th><th>Değer</th><th>Hesaplanamama nedeni</th></tr></thead><tbody>{Object.entries(sensor.statistics).map(([key, stat]) => <tr key={key}><td>{statisticNames[key] ?? key}</td><td>{number(stat)}</td><td>{stat.reason ?? '—'}</td></tr>)}</tbody></table>
      <h3>Otokorelasyon (ACF)</h3><p>Gecikme örnek sayısıdır. Düzensiz zaman ekseninde ACF hesaplanmaz.</p>
      <table><thead><tr><th>Gecikme</th><th>Otokorelasyon</th><th>Hesaplanamama nedeni</th></tr></thead><tbody>{Object.entries(sensor.autocorrelation).map(([lag, stat]) => <tr key={lag}><td>{lag}</td><td>{number(stat)}</td><td>{stat.reason ?? '—'}</td></tr>)}</tbody></table>
    </>}
  </details>;
}

export function OperatingModesPage({ connected, onStarted }: { connected: boolean; onStarted: () => void }) {
  const [sources, setSources] = useState<{ source_id: string; sensors: string[]; row_limit: number; entity_required: boolean }[]>([]);
  const [sourceId, setSourceId] = useState('');
  const [features, setFeatures] = useState<string[]>([]);
  const [rowLimit, setRowLimit] = useState(512);
  const [trainRows, setTrainRows] = useState(192);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [entity, setEntity] = useState('');
  useEffect(() => { if (connected) request<{ items: typeof sources }>('/mode-sources').then(result => setSources(result.items)).catch(cause => setSourceError(String(cause))); }, [connected]);
  const [scenario, setScenario] = useState('healthy_multiple');
  const [seed, setSeed] = useState(0);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [partition, setPartition] = useState('train');
  const [methods, setMethods] = useState(['lsh', 'optics', 'som']);
  const [k, setK] = useState(3);
  const [parameters, setParameters] = useState(defaultParameters);
  const configurationError = parameterError(parameters);
  const [wall, setWall] = useState(600);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function action(work: () => Promise<void>) { setBusy(true); setError(null); try { await work(); } catch (cause) { setError(cause instanceof Error ? cause.message : 'İşlem başarısız'); } finally { setBusy(false); } }
  async function statistics(item: Snapshot, part: string) { setSummary(await request<Summary>(`/mode-snapshots/${item.snapshot_sha256}/statistics?partition=${part}`)); }
  function download() { const url = URL.createObjectURL(new Blob([JSON.stringify({ snapshot, partition, summary }, null, 2)], { type: 'application/json' })); const link = document.createElement('a'); link.href = url; link.download = `mode-statistics-${snapshot?.snapshot_sha256.slice(0, 12)}.json`; link.click(); URL.revokeObjectURL(url); }
  return <div className="page-grid">
    <section className="panel"><div className="panel-heading"><div><h2>Çalışma modları ve OMR</h2><p>Değişmez veri → eğitim modları → en yakın komşular → ölçülen karşılaştırma</p></div></div>
      <p>{snapshot?.source_kind === 'private_database' ? 'Seçilen PostgreSQL verisi değişmez bir snapshot olarak analiz edilir.' : 'Sentetik senaryolar tekrar üretilebilir test verisidir; gerçek endüstriyel veri kabulü değildir.'} Parametre taraması model çağrısı yapmaz; adaylar Director ve Scorer akışında ölçülür.</p>
      {!connected && <p role="status">Lab API bağlı değil. Veri üretimi ve deney başlatma kullanılamıyor.</p>}
      <h3>Yapılandırılmış PostgreSQL kaynağı</h3><p>Bağlantı bilgileri sunucuda kalır. Yalnız izin verilen görünüm ve sensörler seçilebilir.</p>
      {sources.length ? <div className="run-form"><label>Kaynak<select value={sourceId} onChange={e => { setSourceId(e.target.value); const source = sources.find(item => item.source_id === e.target.value); setFeatures(source?.sensors ?? []); const limit = Math.min(512, source?.row_limit ?? 512); setRowLimit(limit); setTrainRows(Math.min(192, Math.floor(limit / 2))); }}><option value="">Kaynak seçin</option>{sources.map(source => <option key={source.source_id}>{source.source_id}</option>)}</select></label><fieldset><legend>Özellikler ({features.length}/50)</legend>{sources.find(source => source.source_id === sourceId)?.sensors.map(sensor => <label key={sensor}><input type="checkbox" checked={features.includes(sensor)} onChange={e => setFeatures(values => e.target.checked ? [...values, sensor] : values.filter(value => value !== sensor))}/>{sensor}</label>)}</fieldset><label>Satır sınırı<input type="number" min="32" max={sources.find(source => source.source_id === sourceId)?.row_limit ?? 4096} value={rowLimit} onChange={e => setRowLimit(Number(e.target.value))}/></label><label>Eğitim satırları<input type="number" min="16" max={rowLimit - 1} value={trainRows} onChange={e => setTrainRows(Number(e.target.value))}/></label><label>Başlangıç (UTC ISO)<input value={start} placeholder="2026-01-01T00:00:00Z" onChange={e => setStart(e.target.value)}/></label><label>Bitiş (UTC ISO)<input value={end} placeholder="2026-01-02T00:00:00Z" onChange={e => setEnd(e.target.value)}/></label><label>Varlık<input value={entity} onChange={e => setEntity(e.target.value)}/></label><button className="button secondary" disabled={busy || !sourceId || !start || !end || !features.length || trainRows >= rowLimit} onClick={() => void action(async () => { const source = sources.find(item => item.source_id === sourceId)!; const item = await request<Snapshot>('/mode-snapshots/database', post({ source_id: sourceId, sensors: features, start_utc: start, end_utc: end, entity: source.entity_required ? entity : null, row_limit: Math.min(rowLimit, source.row_limit), train_rows: trainRows })); setSnapshot(item); await statistics(item, partition); })}>Kaynak snapshot al</button></div> : <p>{sourceError ?? 'İzin verilen veri kaynağı henüz yapılandırılmadı.'}</p>}
      <h3>Sentetik kaynak</h3>
      <div className="run-form"><label>Senaryo<select value={scenario} onChange={e => setScenario(e.target.value)}>{scenarios.map(value => <option key={value}>{value}</option>)}</select></label><label>Kaynak seed<input type="number" min="0" max="4294967295" value={seed} onChange={e => setSeed(Number(e.target.value))}/></label>
        <button className="button primary" disabled={busy || !connected} onClick={() => void action(async () => { const item = await request<Snapshot>('/mode-snapshots', post({ scenario, seed, train_rows: 192, evaluation_rows: 96 })); setSnapshot(item); await statistics(item, partition); })}>Sentetik snapshot üret</button></div>
      {error && <p className="field-error" role="alert">{error}</p>}
      {snapshot && <><p><code>{snapshot.snapshot_sha256}</code></p><p>{snapshot.rows} satır · {snapshot.sensors.length} sensör · eğitim {snapshot.train_rows} / değerlendirme {snapshot.evaluation_rows}</p><p>{snapshot.first_utc} — {snapshot.last_utc}</p><p>Kurulum: {snapshot.readiness === 'ready' ? 'Scorer hazır' : 'Scorer kurulumu bekleniyor'}. Eğitimden türetilen embargo, değerlendirme başlangıcından ayrıca çıkarılır.</p>
        <button className="button secondary" disabled={busy || !snapshot.scoring_available || snapshot.readiness === 'ready'} onClick={() => void action(async () => setSnapshot(await request<Snapshot>(`/mode-snapshots/${snapshot.snapshot_sha256}/install`, post({}))))}>Scorer için hazırla</button>{!snapshot.scoring_available && <p>Bu özel veri kaynağında güvenilir değerlendirme etiketleri yok. Tanımlayıcı analiz hazır; Scorer ölçümü kullanılamıyor.</p>}
      </>}
    </section>
    {snapshot && <section className="panel"><div className="panel-heading"><h2>Tanımlayıcı istatistik</h2><select aria-label="Veri dönemi" value={partition} disabled={busy} onChange={e => { setPartition(e.target.value); void action(() => statistics(snapshot, e.target.value)); }}><option value="train">Eğitim</option><option value="evaluation">Değerlendirme</option><option value="all">Tümü</option></select><button className="button secondary" onClick={download} disabled={!summary}>JSON indir</button></div><p>Değerlendirme istatistikleri kullanıcı içindir; öneri sağlayıcısına aktarılmaz. Eksik değerler doldurulmaz.</p>
      {summary && <><p>{summary.rows} satır · zaman ekseni {summary.timeline.regular ? 'düzenli' : 'düzensiz'} · {summary.timeline.gap_count} boşluk</p><div style={{ overflowX: 'auto' }}><table><thead><tr><th>Sensör</th><th>Geçerli / eksik</th><th>Ortalama</th><th>Std</th><th>Min</th><th>Medyan</th><th>Max</th><th>Dağılım</th></tr></thead><tbody>{summary.sensors.map(sensor => <tr key={sensor.name}><td>{sensor.name}</td><td>{sensor.valid} / {sensor.missing + sensor.nonfinite}</td>{['mean', 'std', 'minimum', 'median', 'maximum'].map(key => <td key={key} title={sensor.statistics[key]?.reason ?? ''}>{number(sensor.statistics[key])}</td>)}<td><svg role="img" aria-label={`${sensor.name} histogram`} width="160" height="40" viewBox="0 0 160 40">{sensor.histogram_counts.map((count, index, counts) => <rect key={index} x={index * 160 / counts.length} y={40 - 38 * count / Math.max(...counts, 1)} width={150 / counts.length} height={38 * count / Math.max(...counts, 1)} fill="currentColor"><title>{count} örnek · {sensor.histogram_edges[index]} — {sensor.histogram_edges[index + 1]}</title></rect>)}</svg></td></tr>)}</tbody></table></div><h3>Sensör ilişkileri</h3><table><thead><tr><th>Çift</th><th>Pearson</th><th>Spearman</th><th>Geçerli çift</th></tr></thead><tbody>{summary.pairs.map(pair => <tr key={`${pair.first}:${pair.second}`}><td>{pair.first} / {pair.second}</td><td title={pair.pearson.reason ?? ''}>{number(pair.pearson)}</td><td title={pair.spearman.reason ?? ''}>{number(pair.spearman)}</td><td>{pair.valid_pairs}</td></tr>)}</tbody></table><StatisticsDetails summary={summary}/>{summary.notes.map(note => <p key={note}>{note}</p>)}</>}
    </section>}
    {snapshot && <section className="panel"><h2>Deterministik parametre karşılaştırması</h2><p>Seçilen her yöntem bir adaydır. Seed tekrarları, baseline, süre sınırı, iptal ve rapor mevcut deney akışı tarafından yönetilir. Sonuçlar ölçülmeden başarı gösterilmez.</p><div className="run-form">{['lsh', 'optics', 'som'].map(method => <label key={method}><input type="checkbox" checked={methods.includes(method)} onChange={e => setMethods(values => e.target.checked ? [...values, method] : values.filter(value => value !== method))}/>{method.toUpperCase()}</label>)}<label>Komşu sayısı k<input type="number" min="1" max="128" value={k} onChange={e => setK(Number(e.target.value))}/></label><label>Süre bütçesi (saniye)<input type="number" min="1" max="14400" value={wall} onChange={e => setWall(Number(e.target.value))}/></label><ModeParameters methods={methods} values={parameters} onChange={setParameters}/>{configurationError && <p role="alert" className="field-error">{configurationError}</p>}<button className="button primary" disabled={busy || !connected || !methods.length || !!configurationError || !Number.isInteger(k) || k < 1 || k > 128 || snapshot.readiness !== 'ready'} onClick={() => void action(async () => { await request('/mode-experiments', post({ idempotency_key: crypto.randomUUID(), snapshot_sha256: snapshot.snapshot_sha256, configurations: methods.map(method => ({ ...parameters, method, k, seed })), wall_seconds: wall })); onStarted(); })}>0 token ile ölçümü başlat</button></div></section>}
  </div>;
}
