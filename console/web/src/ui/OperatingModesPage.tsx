import { useEffect, useRef, useState } from 'react';
import type { FieldIntent, PriorExperience } from './types';
import { ModeStreamForm } from './ModeStreamForm';
import { request } from './api';
import { requestId } from './requestId';
import { defaultParameters, ModeParameters, parameterError } from './ModeParameters';

type PublicSource = { source_id: string; dataset_id: string; task_id: string; source_version: string; license_id: string; usage_profile: 'noncommercial_research'; source_manifest_sha256: string };
type PublicProvenance = { dataset_id: string; split_id: string; session_id: string; source_manifest_sha256: string; source_revision: string; license_id: string; attribution: string; access_terms: string; usage_profile: 'noncommercial_research' };
type Snapshot = { source_kind: string; scoring_available: boolean; snapshot_sha256: string; sensors: string[]; rows: number; train_rows: number; evaluation_rows: number; readiness: string; first_utc: string | null; last_utc: string | null; time_axis?: string; source_time_kind?: string; fit_policy?: string; source_ranges?: { train: [number, number]; evaluation: [number, number] }; source?: PublicSource; provenance?: PublicProvenance; source_suite_manifest_sha256?: string; original_task_sha256?: string; profile_sha256?: string; semantics_sha256?: string; binding_sha256?: string };
const object = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);
const hash = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
function validSnapshot(value: unknown, digest: string): value is Snapshot {
  if (!object(value) || value.snapshot_sha256 !== digest || !hash(value.snapshot_sha256)
    || typeof value.source_kind !== 'string' || !['synthetic', 'private_database', 'public_dev'].includes(value.source_kind)
    || typeof value.scoring_available !== 'boolean' || typeof value.readiness !== 'string'
    || !Array.isArray(value.sensors) || !value.sensors.length || value.sensors.length > 50
    || !value.sensors.every(sensor => typeof sensor === 'string' && sensor.length > 0)
    || new Set(value.sensors).size !== value.sensors.length
    || !['rows', 'train_rows', 'evaluation_rows'].every(key => typeof value[key] === 'number' && Number.isSafeInteger(value[key]) && (value[key] as number) > 0)
    || value.rows !== (value.train_rows as number) + (value.evaluation_rows as number)
    || !(value.first_utc === null || typeof value.first_utc === 'string') || !(value.last_utc === null || typeof value.last_utc === 'string')) return false;
  if (value.source_kind !== 'public_dev') return true;
  const source = value.source, provenance = value.provenance, ranges = value.source_ranges;
  if (!object(source) || !object(provenance) || !object(ranges)
    || !['source_id', 'dataset_id', 'task_id', 'source_version', 'license_id'].every(key => typeof source[key] === 'string' && source[key].length > 0)
    || !['dataset_id', 'split_id', 'session_id', 'source_revision', 'license_id', 'attribution', 'access_terms'].every(key => typeof provenance[key] === 'string' && provenance[key].length > 0)
    || source.usage_profile !== 'noncommercial_research' || provenance.usage_profile !== source.usage_profile
    || provenance.dataset_id !== source.dataset_id || provenance.source_revision !== source.source_version
    || provenance.license_id !== source.license_id || !hash(source.source_manifest_sha256)
    || provenance.source_manifest_sha256 !== source.source_manifest_sha256
    || !['source_suite_manifest_sha256', 'original_task_sha256', 'profile_sha256', 'semantics_sha256', 'binding_sha256'].every(key => hash(value[key]))
    || value.first_utc !== null || value.last_utc !== null || value.time_axis !== 'source_order_index' || value.source_time_kind !== 'naive'
    || value.fit_policy !== 'label_blind_source_order;normal_fit_not_guaranteed' || value.scoring_available !== true
    || !['ready', 'pending_scorer_installation'].includes(value.readiness) || (value.rows as number) > 4096
    || !['train', 'evaluation'].every(key => Array.isArray(ranges[key]) && ranges[key].length === 2
      && ranges[key].every(index => typeof index === 'number' && Number.isSafeInteger(index) && index >= 0))) return false;
  const train = ranges.train as number[], evaluation = ranges.evaluation as number[];
  return train[1] - train[0] === value.train_rows && evaluation[1] - evaluation[0] === value.evaluation_rows && train[1] <= evaluation[0];
}
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

export function OperatingModesPage({ connected, modelRunsEnabled = false, priorExperience, onClearPrior, fieldIntent, onClearFieldIntent, onEditFieldIntent, onStarted }: { connected: boolean; modelRunsEnabled?: boolean; priorExperience: PriorExperience | null; onClearPrior: () => void; fieldIntent: FieldIntent | null; onClearFieldIntent: () => void; onEditFieldIntent: () => void; onStarted: () => void }) {
  const pending = useRef<{ serialized: string; key: string } | null>(null);
  async function startExperiment(path: string, body: Record<string, unknown>) {
    const serialized = JSON.stringify({ path, body });
    if (pending.current?.serialized !== serialized) pending.current = { serialized, key: requestId() };
    await request(path, post({ ...body, idempotency_key: pending.current.key }));
    pending.current = null;
    onStarted();
  }
  const fieldBody = fieldIntent ? { field_intent: fieldIntent } : {};
  const historyBody = priorExperience && connected ? { prior_experience: priorExperience } : {};
  const [snapshotDigest, setSnapshotDigest] = useState('');
  const [sources, setSources] = useState<{ source_id: string; sensors: string[]; row_limit: number; entity_required: boolean; source_kind?: string }[]>([]);
  const [sourceId, setSourceId] = useState('');
  const [features, setFeatures] = useState<string[]>([]);
  const [rowLimit, setRowLimit] = useState(512);
  const [trainRows, setTrainRows] = useState(192);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [entity, setEntity] = useState('');
  useEffect(() => { if (connected) request<{ items: typeof sources }>('/mode-sources').then(result => setSources(result.items.filter(item => !item.source_kind || item.source_kind === 'private_database'))).catch(cause => setSourceError(String(cause))); }, [connected]);
  const [scenario, setScenario] = useState('healthy_multiple');
  const [seed, setSeed] = useState(0);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const publicDev = snapshot?.source_kind === 'public_dev';
  const [summary, setSummary] = useState<Summary | null>(null);
  const [partition, setPartition] = useState('train');
  const [methods, setMethods] = useState(['lsh', 'optics', 'som']);
  const [k, setK] = useState(3);
  const [parameters, setParameters] = useState(defaultParameters);
  const configurationError = parameterError(parameters);
  const [wall, setWall] = useState(600);
  const [experimentKind, setExperimentKind] = useState<'grid' | 'agent'>('grid');
  const [proposals, setProposals] = useState(6);
  const [agentWall, setAgentWall] = useState(7200);
  const [tokens, setTokens] = useState(180000);
  const [profileSet, setProfileSet] = useState<'smoke' | 'research'>('research');
  const boundedInteger = (value: number, maximum: number) => Number.isInteger(value) && value >= 1 && value <= maximum;
  const agentBudgetValid = boundedInteger(proposals, 35) && boundedInteger(agentWall, 14400) && boundedInteger(tokens, 350000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function action(work: () => Promise<void>) { setBusy(true); setError(null); try { await work(); } catch (cause) { setError(cause instanceof Error ? cause.message : 'İşlem başarısız'); } finally { setBusy(false); } }
  async function statistics(item: Snapshot, part: string) { setSummary(await request<Summary>(`/mode-snapshots/${item.snapshot_sha256}/statistics?partition=${part}`)); }
  function download() { const url = URL.createObjectURL(new Blob([JSON.stringify({ snapshot, partition, summary }, null, 2)], { type: 'application/json' })); const link = document.createElement('a'); link.href = url; link.download = `mode-statistics-${snapshot?.snapshot_sha256.slice(0, 12)}.json`; link.click(); URL.revokeObjectURL(url); }
  return <div className="page-grid">
    <section className="panel" aria-labelledby="mode-field-intent-title"><h2 id="mode-field-intent-title">Saha araştırma amacı</h2>{fieldIntent ? <><dl><dt>Varlık etiketi · kullanıcı beyanı</dt><dd>{fieldIntent.asset_id}</dd><dt>Araştırma amacı</dt><dd>{fieldIntent.goal_kind === 'digital_twin' ? 'Dijital ikiz araştırması' : 'Kestirimci bakım araştırması'}</dd><dt>Çalışma hedefi</dt><dd>{fieldIntent.objective}</dd></dl><p>Seçilen snapshot ile yeni parametre karşılaştırmasına veya agent araştırmasına danışma bilgisi olarak eklenir. Yetkili kaynak/varlık eşlemesi ve bakım aksiyonu değildir; yöntem, skor ve bütçe değişmez. Aşağıdaki OMR tanı akışına eklenmez.</p></> : <p>Saha amacı eklenmedi. Veri ve yöntem seçimiyle mevcut deney akışına devam edebilirsiniz.</p>}<div className="agent-actions"><button className="button secondary small" onClick={onEditFieldIntent} disabled={busy}>{fieldIntent ? 'Saha amacını düzenle' : 'Saha amacı ekle'}</button>{fieldIntent && <button className="button secondary small" onClick={onClearFieldIntent} disabled={busy}>Saha amacını kaldır</button>}</div></section>
    {priorExperience && <section className="panel experience-handoff" aria-label="Seçili geçmiş bulgular"><h2>Seçili geçmiş bulgular</h2><p>{priorExperience.records.length} doğrulanmış geliştirme kaydı · kaynak koşu <code>{priorExperience.source_run_id}</code></p><p>Yalnız yeni parametre karşılaştırması veya agent araştırmasına danışma bağlamı olarak eklenir. Geçmiş skorlar yeni ölçümün yerine geçmez; deterministik parametre planı değişmez. Aşağıdaki OMR tanı akışına eklenmez.</p><button className="button secondary small" onClick={onClearPrior} disabled={busy}>Geçmiş bulguları kaldır</button></section>}
    {!publicDev && <ModeStreamForm connected={connected} onStarted={onStarted}/>}
    <section className="panel"><div className="panel-heading"><div><h2>Çalışma modları ve OMR</h2><p>Değişmez veri → eğitim modları → en yakın komşular → ölçülen karşılaştırma</p></div></div>
      <p>{publicDev ? 'Kurulu public DEV snapshot gerçek kaynak verisidir. Yalnız CPU parametre karşılaştırması yapılır; yerel model araştırması ve OMR tanı akışı bu veri yolunda kapalıdır.' : snapshot?.source_kind === 'private_database' ? 'Seçilen PostgreSQL verisi değişmez bir snapshot olarak analiz edilir.' : 'Sentetik senaryolar tekrar üretilebilir test verisidir; gerçek endüstriyel veri kabulü değildir.'} Parametre taraması model çağrısı yapmaz. Agent araştırması yerel modelle sınırlı sayıda yapılandırma önerir; adaylar Director ve Scorer akışında ölçülür.</p>
      {!connected && <p role="status">Lab API bağlı değil. Veri üretimi ve deney başlatma kullanılamıyor.</p>}
      <h3>Yapılandırılmış PostgreSQL kaynağı</h3><p>Bağlantı bilgileri sunucuda kalır. Yalnız izin verilen görünüm ve sensörler seçilebilir.</p>
      {sources.length ? <div className="run-form"><label>Kaynak<select value={sourceId} onChange={e => { setSourceId(e.target.value); const source = sources.find(item => item.source_id === e.target.value); setFeatures(source?.sensors ?? []); const limit = Math.min(512, source?.row_limit ?? 512); setRowLimit(limit); setTrainRows(Math.min(192, Math.floor(limit / 2))); }}><option value="">Kaynak seçin</option>{sources.map(source => <option key={source.source_id}>{source.source_id}</option>)}</select></label><fieldset><legend>Özellikler ({features.length}/50)</legend>{sources.find(source => source.source_id === sourceId)?.sensors.map(sensor => <label key={sensor}><input type="checkbox" checked={features.includes(sensor)} onChange={e => setFeatures(values => e.target.checked ? [...values, sensor] : values.filter(value => value !== sensor))}/>{sensor}</label>)}</fieldset><label>Satır sınırı<input type="number" min="32" max={sources.find(source => source.source_id === sourceId)?.row_limit ?? 4096} value={rowLimit} onChange={e => setRowLimit(Number(e.target.value))}/></label><label>Eğitim satırları<input type="number" min="16" max={rowLimit - 1} value={trainRows} onChange={e => setTrainRows(Number(e.target.value))}/></label><label>Başlangıç (UTC ISO)<input value={start} placeholder="2026-01-01T00:00:00Z" onChange={e => setStart(e.target.value)}/></label><label>Bitiş (UTC ISO)<input value={end} placeholder="2026-01-02T00:00:00Z" onChange={e => setEnd(e.target.value)}/></label><label>Varlık<input value={entity} onChange={e => setEntity(e.target.value)}/></label><button className="button secondary" disabled={busy || !sourceId || !start || !end || !features.length || trainRows >= rowLimit} onClick={() => void action(async () => { const source = sources.find(item => item.source_id === sourceId)!; const item = await request<Snapshot>('/mode-snapshots/database', post({ source_id: sourceId, sensors: features, start_utc: start, end_utc: end, entity: source.entity_required ? entity : null, row_limit: Math.min(rowLimit, source.row_limit), train_rows: trainRows })); setSnapshot(item); await statistics(item, partition); })}>Kaynak snapshot al</button></div> : <p>{sourceError ?? 'İzin verilen veri kaynağı henüz yapılandırılmadı.'}</p>}
      <h3>Kurulu veri kaynağı</h3><p>Operatörün verdiği snapshot SHA-256 ile değişmez kaydı açın. Bu işlem veri içe aktarması veya Scorer kurulumu yapmaz.</p>
      <div className="run-form"><label>Snapshot SHA-256<input value={snapshotDigest} disabled={busy} spellCheck={false} maxLength={64} onChange={event => setSnapshotDigest(event.target.value)} placeholder="64 karakterlik SHA-256"/></label><button className="button secondary" disabled={busy || !connected || !/^[0-9a-f]{64}$/.test(snapshotDigest.trim())} onClick={() => void action(async () => {
        const digest = snapshotDigest.trim();
        const item = await request<unknown>(`/mode-snapshots/${digest}`);
        if (!validSnapshot(item, digest)) throw new Error('Snapshot yanıtının kimliği, kaynak kökeni veya zaman bilgisi doğrulanamadı.');
        if (item.source_kind === 'public_dev') { setExperimentKind('grid'); setMethods(values => values.slice(0, 2)); setWall(value => Math.min(value, 600)); }
        setSnapshot(item); setSummary(null); await statistics(item, partition);
      })}>Kurulu veri kaynağını aç</button></div>
      <h3>Sentetik kaynak</h3>
      <div className="run-form"><label>Senaryo<select value={scenario} onChange={e => setScenario(e.target.value)}>{scenarios.map(value => <option key={value}>{value}</option>)}</select></label><label>Kaynak seed<input type="number" min="0" max="4294967295" value={seed} onChange={e => setSeed(Number(e.target.value))}/></label>
        <button className="button primary" disabled={busy || !connected} onClick={() => void action(async () => { const item = await request<Snapshot>('/mode-snapshots', post({ scenario, seed, train_rows: 192, evaluation_rows: 96 })); setSnapshot(item); await statistics(item, partition); })}>Sentetik snapshot üret</button></div>
      {error && <p className="field-error" role="alert">{error}</p>}
      {snapshot && publicDev && snapshot.source && snapshot.provenance && <div className="experience-handoff"><h3>Gerçek public DEV kaynağı · {snapshot.source.dataset_id}</h3><dl><dt>Kaynak / geliştirme görevi</dt><dd>{snapshot.source.source_id} · {snapshot.source.task_id}</dd><dt>Lisans / kullanım</dt><dd>{snapshot.source.license_id} · Ticari olmayan araştırma</dd><dt>Özgün kaynak sürümü</dt><dd>{snapshot.source.source_version}</dd><dt>Atıf</dt><dd>{snapshot.provenance.attribution}</dd><dt>Erişim koşulları</dt><dd>{snapshot.provenance.access_terms}</dd></dl><details><summary>Özgün kaynak ve türetilmiş bağlama referansları</summary><dl><dt>Özgün split / oturum</dt><dd>{snapshot.provenance.split_id} · {snapshot.provenance.session_id}</dd><dt>Kaynak manifest SHA-256</dt><dd>{snapshot.source.source_manifest_sha256}</dd><dt>Kaynak suite manifest SHA-256</dt><dd>{snapshot.source_suite_manifest_sha256}</dd><dt>Özgün görev SHA-256</dt><dd>{snapshot.original_task_sha256}</dd><dt>Profil SHA-256</dt><dd>{snapshot.profile_sha256}</dd><dt>Semantik SHA-256</dt><dd>{snapshot.semantics_sha256}</dd><dt>Türetilmiş bağlama SHA-256</dt><dd>{snapshot.binding_sha256}</dd></dl></details><p>Bu kaynak lisans/köken kaydı eğitim veya dışa aktarma izni yerine geçmez. Public DEV karşılaştırması bağımsız holdout kabulü değildir.</p></div>}
      {snapshot && <><p><code>{snapshot.snapshot_sha256}</code></p><p>{snapshot.rows} satır · {snapshot.sensors.length} sensör · {publicDev ? 'fit / kaynak sırası referansı' : 'eğitim'} {snapshot.train_rows} / değerlendirme {snapshot.evaluation_rows}</p>{publicDev ? <><p>Zaman ekseni: kaynak satır sırası. Kaynak zamanları saat dilimi içermeyen değerlerdir (naive); UTC zamanı üretilmez.</p><p>Fit dilimi kaynak sırasına göre seçilir; garantili normal işletim dilimi değildir. Etiketler fit seçimini yönlendirmez.</p>{snapshot.source_ranges && <p>Kaynak satır aralıkları (başlangıç dahil, bitiş hariç): fit [{snapshot.source_ranges.train.join(', ')}), değerlendirme [{snapshot.source_ranges.evaluation.join(', ')}).</p>}</> : <p>{snapshot.first_utc} — {snapshot.last_utc}</p>}<p>Kurulum: {snapshot.readiness === 'ready' ? 'Scorer hazır' : 'Scorer kurulumu bekleniyor'}. {publicDev ? 'Özgün kaynak split ve embargo korunur; ek satır çıkarılmaz.' : 'Eğitimden türetilen embargo, değerlendirme başlangıcından ayrıca çıkarılır.'}</p>
        {!publicDev && <button className="button secondary" disabled={busy || !snapshot.scoring_available || snapshot.readiness === 'ready'} onClick={() => void action(async () => setSnapshot(await request<Snapshot>(`/mode-snapshots/${snapshot.snapshot_sha256}/install`, post({}))))}>Scorer için hazırla</button>}{publicDev && snapshot.readiness !== 'ready' && <p>Scorer kurulumu operatör tarafından tamamlanmalı; bu görünümden kurulmaz.</p>}{!snapshot.scoring_available && <p>Bu veri kaynağında güvenilir değerlendirme etiketleri yok. Tanımlayıcı analiz hazır; Scorer ölçümü kullanılamıyor.</p>}
      </>}
    </section>
    {snapshot && <section className="panel"><div className="panel-heading"><h2>Tanımlayıcı istatistik</h2><select aria-label="Veri dönemi" value={partition} disabled={busy} onChange={e => { setPartition(e.target.value); void action(() => statistics(snapshot, e.target.value)); }}><option value="train">{publicDev ? 'Fit / kaynak sırası referansı' : 'Eğitim'}</option><option value="evaluation">Değerlendirme</option><option value="all">Tümü</option></select><button className="button secondary" onClick={download} disabled={!summary}>JSON indir</button></div><p>Değerlendirme istatistikleri kullanıcı içindir; öneri sağlayıcısına aktarılmaz. Eksik değerler doldurulmaz.</p>
      {summary && <><p>{summary.rows} satır · {publicDev ? 'Zaman düzeni ve boşluklar ölçülmedi; kaynak satır sırası.' : <>zaman ekseni {summary.timeline.regular ? 'düzenli' : 'düzensiz'} · {summary.timeline.gap_count} boşluk</>}</p><div style={{ overflowX: 'auto' }}><table><thead><tr><th>Sensör</th><th>Geçerli / eksik</th><th>Ortalama</th><th>Std</th><th>Min</th><th>Medyan</th><th>Max</th><th>Dağılım</th></tr></thead><tbody>{summary.sensors.map(sensor => <tr key={sensor.name}><td>{sensor.name}</td><td>{sensor.valid} / {sensor.missing + sensor.nonfinite}</td>{['mean', 'std', 'minimum', 'median', 'maximum'].map(key => <td key={key} title={sensor.statistics[key]?.reason ?? ''}>{number(sensor.statistics[key])}</td>)}<td><svg role="img" aria-label={`${sensor.name} histogram`} width="160" height="40" viewBox="0 0 160 40">{sensor.histogram_counts.map((count, index, counts) => <rect key={index} x={index * 160 / counts.length} y={40 - 38 * count / Math.max(...counts, 1)} width={150 / counts.length} height={38 * count / Math.max(...counts, 1)} fill="currentColor"><title>{count} örnek · {sensor.histogram_edges[index]} — {sensor.histogram_edges[index + 1]}</title></rect>)}</svg></td></tr>)}</tbody></table></div><h3>Sensör ilişkileri</h3><table><thead><tr><th>Çift</th><th>Pearson</th><th>Spearman</th><th>Geçerli çift</th></tr></thead><tbody>{summary.pairs.map(pair => <tr key={`${pair.first}:${pair.second}`}><td>{pair.first} / {pair.second}</td><td title={pair.pearson.reason ?? ''}>{number(pair.pearson)}</td><td title={pair.spearman.reason ?? ''}>{number(pair.spearman)}</td><td>{pair.valid_pairs}</td></tr>)}</tbody></table><StatisticsDetails summary={summary}/>{summary.notes.map(note => <p key={note}>{note}</p>)}</>}
    </section>}
    {snapshot && <section className="panel"><h2>Çalışma modu deneyi</h2>
      <label>Deney türü<select value={experimentKind} disabled={busy} onChange={e => setExperimentKind(e.target.value as 'grid' | 'agent')}><option value="grid">Deterministik parametre karşılaştırması (0 token)</option><option value="agent" disabled={publicDev}>Agent araştırması (yerel model)</option></select></label>
      <p>Mod toleransı → en yakın komşu normal değer tahmini → sürekli sensör residual'ları ve OMR. Mod uzaklığı, SOM uzaklığı ve OMR ayrı ölçümlerdir. Sonuçlar ölçülmeden başarı gösterilmez.</p>
      {publicDev || experimentKind === 'grid' ? <>{publicDev && <p>Public DEV sınırı: en fazla 2 aday, 600 saniye, 0 model tokenı. Fit/geliştirme ölçümü holdout veya genel başarı kanıtı değildir.</p>}<p>Seçilen her yöntem bir adaydır. Seed tekrarları, baseline, süre sınırı, iptal ve rapor mevcut deney akışı tarafından yönetilir.</p><div className="run-form">{['lsh', 'optics', 'som'].map(method => <label key={method}><input type="checkbox" disabled={publicDev && !methods.includes(method) && methods.length >= 2} checked={methods.includes(method)} onChange={e => setMethods(values => e.target.checked ? [...values, method] : values.filter(value => value !== method))}/>{method.toUpperCase()}</label>)}<label>Komşu sayısı k<input type="number" min="1" max="128" value={k} onChange={e => setK(Number(e.target.value))}/></label><label>Süre bütçesi (saniye)<input type="number" min="1" max={publicDev ? 600 : 14400} value={wall} onChange={e => setWall(Number(e.target.value))}/></label><ModeParameters methods={methods} values={parameters} onChange={setParameters}/>{configurationError && <p role="alert" className="field-error">{configurationError}</p>}<button className="button primary" disabled={busy || !connected || !methods.length || (publicDev && methods.length > 2) || !!configurationError || !boundedInteger(k, 128) || !boundedInteger(wall, publicDev ? 600 : 14400) || snapshot.readiness !== 'ready'} onClick={() => void action(async () => { await startExperiment('/mode-experiments', { snapshot_sha256: snapshot.snapshot_sha256, configurations: methods.map(method => ({ ...parameters, method, k, seed })), wall_seconds: wall, ...historyBody, ...fieldBody }); })}>0 token ile ölçümü başlat</button></div></> : <>
        <p>Yerel model, sunucunun doğruladığı LSH, OPTICS ve SOM yapılandırmaları arasından öneri seçer. Komşu sayısı ve ağırlığı, uzaklık metriği, mod toleransı, kalibrasyon, alarm ve yöntem hiperparametreleri bu aramanın parçasıdır. Öneri yapılandırma olarak doğrulanır; modelden kod istenmez. Değerlendirme istatistikleri modele aktarılmaz.</p>
        <p>Süre ve token bütçeleri bütün araştırmayı sınırlar; model çağrıları mevcut kaynak kabulü ve GPU sıra kurallarına tabidir.</p>
        {!modelRunsEnabled && <p role="status">Yerel model araştırmaları konsolda kapalı. Başlatmak için sunucunun mevcut model çalıştırma izni gerekir.</p>}
        <div className="run-form"><label>Öneri sayısı<input type="number" min="1" max="35" value={proposals} onChange={e => setProposals(Number(e.target.value))}/></label><label>Toplam süre bütçesi (saniye)<input type="number" min="1" max="14400" value={agentWall} onChange={e => setAgentWall(Number(e.target.value))}/></label><label>Toplam model token bütçesi<input type="number" min="1" max="350000" value={tokens} onChange={e => setTokens(Number(e.target.value))}/></label><label>Deney profili<select value={profileSet} onChange={e => setProfileSet(e.target.value as 'smoke' | 'research')}><option value="research">Araştırma</option><option value="smoke">Küçük doğrulama</option></select></label>
          {!agentBudgetValid && <p role="alert" className="field-error">Öneri 1–35, süre 1–14400 saniye ve token 1–350000 aralığında tam sayı olmalıdır.</p>}
          <button className="button primary" disabled={busy || !connected || !modelRunsEnabled || !agentBudgetValid || snapshot.readiness !== 'ready'} onClick={() => void action(async () => { if (publicDev) throw new Error('Public DEV kaynağında yerel model araştırması kapalı.'); if (!modelRunsEnabled) throw new Error('Yerel model araştırmaları kapalı.'); await startExperiment('/mode-agent-experiments', { snapshot_sha256: snapshot.snapshot_sha256, experiments: proposals, wall_seconds: agentWall, model_tokens: tokens, profile_set: profileSet, ...historyBody, ...fieldBody }); })}>Yerel modelle araştırmayı başlat</button>
        </div><p>Koşu Deneyler ekranından izlenir, durdurulur ve raporu açılır. Başlatma yanıtı araştırma başarısı kanıtı değildir.</p>
      </>}
    </section>}
  </div>;
}
