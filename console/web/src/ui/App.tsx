import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError } from './api';
import { ModeDiagnostics } from './ModeDiagnostics';
import { OperatingModesPage } from './OperatingModesPage';
import type { AcceptanceItem, AcceptanceStatus, Check, EvidenceResponse, Overview, Run, Suite } from './types';

type RunForm = { purpose: 'baseline' | 'research'; suite: string; experiments: number; wall_seconds: number; model_tokens: number };
const purposeText = (purpose: Run['purpose']) => purpose === 'baseline' ? 'CPU baseline' : purpose === 'mode-grid' ? 'Parametre taraması · 0 token' : purpose === 'research' ? 'Araştırma' : 'Tür bilinmiyor';
type Page = 'overview' | 'acceptance' | 'experiments' | 'system' | 'modes';
type Notice = { kind: 'error' | 'success'; text: string } | null;
const statusText: Record<AcceptanceStatus, string> = { passed: 'Geçti', partial: 'Kısmi', open: 'Açık' };
const stateText: Record<string, string> = { queued: 'Kuyrukta', running: 'Çalışıyor', stop_requested: 'Durdurma bekleniyor', passed: 'Başarılı', failed: 'Başarısız', completed: 'Tamamlandı', stopped: 'Durduruldu', cancelled: 'İptal edildi', error: 'Hata' };
const activeRunStates = new Set(['queued', 'running', 'stop_requested']);
const stoppableRunStates = new Set(['queued', 'running']);
const readableState = (state: string) => stateText[state] ?? state;
const reasonText = (reason: string | null | undefined) => reason?.toLocaleLowerCase('en-US').includes('lab api is not configured') ? 'Lab API henüz yapılandırılmadı.' : reason;
function canRunSuite(lab: Overview['lab'] | undefined, suite: Suite | undefined, purpose: RunForm['purpose'] = 'research') {
  return !!lab?.connected && !!suite && (purpose === 'baseline' || suite.provider !== 'local-qwen' || lab.model_runs_enabled);
}
function canRunAnySuite(lab: Overview['lab'] | undefined) {
  return !!lab?.suites.some(suite => canRunSuite(lab, suite, 'baseline'));
}
const fmtBytes = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? null : value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(1)} GiB` : `${(value / 1024 ** 2).toFixed(0)} MiB`;
const fmtTime = (value: string | null | undefined) => value ? new Date(value).toLocaleString('tr-TR', { dateStyle: 'medium', timeStyle: 'short' }) : '—';
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function Icon({ name }: { name: string }) {
  const paths: Record<string, React.ReactNode> = {
    home: <><path d="m3 10 9-7 9 7"/><path d="M5 9v11h14V9M9 20v-7h6v7"/></>,
    check: <><path d="M9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></>,
    flask: <><path d="M9 3h6M10 3v7l-5.5 9.2A1.8 1.8 0 0 0 6.1 22h11.8a1.8 1.8 0 0 0 1.6-2.8L14 10V3M8 16h8"/></>,
    settings: <><circle cx="12" cy="12" r="5"/><path d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42"/></>,
    arrow: <><path d="M5 12h14M13 6l6 6-6 6"/></>,
    close: <><path d="m18 6-12 12M6 6l12 12"/></>,
    refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.6 9a7 7 0 0 1 11.7-2L20 12M4 12l2.7 5a7 7 0 0 0 11.7-2"/></>,
    file: <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h8"/></>,
  };
  return <svg className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] ?? paths.file}</svg>;
}

function App() {
  const [page, setPage] = useState<Page>('overview');
  const [overview, setOverview] = useState<Overview | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [checks, setChecks] = useState<Check[]>([]);
  const [apiError, setApiError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [filter, setFilter] = useState<'all' | AcceptanceStatus>('all');
  const [selected, setSelected] = useState<AcceptanceItem | null>(null);
  const [evidence, setEvidence] = useState<{ ref: AcceptanceItem['evidence'][number]; data?: EvidenceResponse; error?: string } | null>(null);
  const [watchId, setWatchId] = useState('');
  const [runId, setRunId] = useState<string | null>(null);
  const [runDetail, setRunDetail] = useState<Run | null>(null);
  const [report, setReport] = useState<unknown>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<RunForm>({ purpose: 'baseline', suite: '', experiments: 1, wall_seconds: 300, model_tokens: 0 });
  const [formError, setFormError] = useState<string | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const results = await Promise.allSettled([api.overview(signal), api.runs(signal), api.checks(signal)]);
    if (signal?.aborted) return;
    const overviewResult = results[0];
    if (overviewResult.status === 'fulfilled') {
      setOverview(overviewResult.value); setApiError(null);
      setForm(current => current.suite || !overviewResult.value.lab.suites.length ? current : { ...current, suite: overviewResult.value.lab.suites[0].suite_id });
    } else if (!(overviewResult.reason instanceof DOMException && overviewResult.reason.name === 'AbortError')) {
      setOverview(null);
      setApiError(overviewResult.reason instanceof Error ? overviewResult.reason.message : 'Yerel API yanıt vermedi.');
    }
    if (results[1].status === 'fulfilled') setRuns(results[1].value.items);
    else if (!(results[1].reason instanceof DOMException && results[1].reason.name === 'AbortError')) setRuns(items => items.map(item => ({ ...item, unavailable: true })));
    if (results[2].status === 'fulfilled') setChecks(results[2].value.items);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let timer = 0;
    let stopped = false;
    const tick = async () => {
      await refresh(controller.signal);
      if (!stopped && !controller.signal.aborted) timer = window.setTimeout(() => void tick(), 5000);
    };
    void tick();
    return () => { stopped = true; controller.abort(); window.clearTimeout(timer); };
  }, [refresh]);

  useEffect(() => {
    if (!runId) return;
    const controller = new AbortController();
    let timer = 0;
    let stopped = false;
    const poll = async () => {
      let keepPolling = true;
      try {
        const current = await api.run(runId, controller.signal);
        if (!controller.signal.aborted) { setRunDetail(current); keepPolling = activeRunStates.has(current.state); }
      } catch (error) { if (!controller.signal.aborted) setNotice({ kind: 'error', text: errorText(error) }); }
      if (!stopped && !controller.signal.aborted && keepPolling) timer = window.setTimeout(() => void poll(), 2500);
    };
    void poll();
    return () => { stopped = true; controller.abort(); window.clearTimeout(timer); };
  }, [runId]);

  const acceptanceItems = overview?.acceptance.items ?? [];
  const filteredItems = useMemo(() => acceptanceItems.filter(item => filter === 'all' || item.status === filter), [acceptanceItems, filter]);
  const activeCheck = checks.find(item => item.state === 'queued' || item.state === 'running');
  const selectedSuite = overview?.lab.suites.find(item => item.suite_id === form.suite);

  async function startCheck() {
    setPage('system');
    setBusy(true); setNotice(null);
    try {
      const created = await api.startCheck();
      setChecks(items => [{ id: created.id, state: created.state, started_at: new Date().toISOString(), finished_at: null, exit_code: null, summary: null, output: null }, ...items]);
      const current = await api.check(created.id);
      setChecks(items => [current, ...items.filter(item => item.id !== current.id)]);
      setNotice({ kind: 'success', text: `CPU kontrolü başlatıldı. Sonuç Sistem ekranında görünecek.` });
      await refresh();
    } catch (error) { setNotice({ kind: 'error', text: errorText(error) }); }
    finally { setBusy(false); }
  }
  async function watchRun(event: React.FormEvent) {
    event.preventDefault(); setNotice(null);
    if (!uuidPattern.test(watchId.trim())) { setNotice({ kind: 'error', text: 'Geçerli bir koşu UUID’si girin.' }); return; }
    setBusy(true);
    try {
      const tracked = await api.watchRun(watchId.trim());
      setRunId(tracked.run_id); setRunDetail(tracked); setWatchId(''); await refresh();
      setNotice({ kind: 'success', text: `Koşu ${tracked.run_id} doğrulanıp izlemeye eklendi.` });
    } catch (error) { setNotice({ kind: 'error', text: errorText(error) }); }
    finally { setBusy(false); }
  }
  async function startRun(event: React.FormEvent) {
    event.preventDefault(); setFormError(null);
    if (!overview?.lab.connected) { setFormError(reasonText(overview?.lab.reason) || 'Lab API bağlı değil; yeni deney başlatılamıyor.'); return; }
    if (!selectedSuite || !canRunSuite(overview.lab, selectedSuite, form.purpose)) { setFormError(reasonText(overview.lab.reason) || 'Seçili suite için model çalıştırma etkin değil.'); return; }
    const withoutKey = {
      track: selectedSuite.track, suite: selectedSuite.suite_id,
      program_version: selectedSuite.program_version,
      budget: { experiments: Number(form.experiments), wall_seconds: Number(form.wall_seconds), model_tokens: Number(form.model_tokens) },
    };
    const serializedRequest = JSON.stringify({ purpose: form.purpose, ...withoutKey });
    let retry: { request: string; key: string } | null = null;
    try { retry = JSON.parse(window.sessionStorage.getItem('lab-console-start-retry') || 'null') as { request: string; key: string } | null; } catch { retry = null; }
    const key = retry?.request === serializedRequest ? retry.key : crypto.randomUUID();
    window.sessionStorage.setItem('lab-console-start-retry', JSON.stringify({ request: serializedRequest, key }));
    setBusy(true);
    try {
      const created = form.purpose === 'baseline'
        ? await api.startBaseline({ suite: selectedSuite.suite_id, program_version: selectedSuite.program_version, idempotency_key: key, budget: { experiments: 0, wall_seconds: Number(form.wall_seconds), model_tokens: 0 } })
        : await api.startRun({ ...withoutKey, idempotency_key: key });
      window.sessionStorage.removeItem('lab-console-start-retry');
      setRunId(created.run_id); setRunDetail({ run_id: created.run_id, state: created.state, purpose: form.purpose });
      setShowForm(false); await refresh();
      setNotice({ kind: 'success', text: `${purposeText(form.purpose)} ${created.run_id} ${created.reused ? 'önceki isteğin sonucu olarak bulundu' : 'başlatıldı'}.` });
    } catch (error) { setFormError(errorText(error)); }
    finally { setBusy(false); }
  }
  async function stopRun(id: string) {
    setBusy(true); setNotice(null);
    try { setRunDetail(await api.stopRun(id)); await refresh(); setNotice({ kind: 'success', text: `Durdurma isteği ${id} için iletildi.` }); }
    catch (error) { setNotice({ kind: 'error', text: errorText(error) }); }
    finally { setBusy(false); }
  }
  async function openReport(id: string) {
    setRunId(id); setRunDetail(runs.find(run => run.run_id === id) ?? null); setReport(null); setNotice(null);
    try { setReport(await api.report(id)); }
    catch (error) { setNotice({ kind: 'error', text: errorText(error) }); }
  }
  async function openEvidence(ref: AcceptanceItem['evidence'][number]) {
    setEvidence({ ref });
    try { setEvidence({ ref, data: await api.evidence(ref.url) }); }
    catch (error) { setEvidence({ ref, error: errorText(error) }); }
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-name">AI Scientist</div><div className="brand-subtitle">Yerel araştırma</div></div>
      <nav className="side-nav" aria-label="Ana gezinme">
        <NavButton name="home" label="Genel bakış" active={page === 'overview'} onClick={() => setPage('overview')} />
        <NavButton name="check" label="Kabul maddeleri" active={page === 'acceptance'} onClick={() => setPage('acceptance')} />
        <NavButton name="flask" label="Çalışma modları" active={page === 'modes'} onClick={() => setPage('modes')} />
        <NavButton name="flask" label="Deneyler" active={page === 'experiments'} onClick={() => setPage('experiments')} />
        <NavButton name="settings" label="Sistem" active={page === 'system'} onClick={() => setPage('system')} />
      </nav>
      <div className="side-footer">{overview ? `v${overview.version}` : 'API bekleniyor'}</div>
    </aside>
    <main className="main">
      <header className="page-header">
        <div><h1>Araştırma kontrol merkezi</h1><p>Tamamlanmayı izle, deneyleri çalıştır, kanıtları incele.</p></div>
        <button className="button primary header-action" onClick={startCheck} disabled={busy || !!activeCheck} title={activeCheck ? 'CPU kontrolü zaten çalışıyor' : undefined}>{busy && !activeCheck ? 'Başlatılıyor…' : activeCheck ? 'Kontrol çalışıyor' : 'Kontrolü çalıştır'}</button>
      </header>
      {notice && <div className={`notice ${notice.kind}`} role="status"><span>{notice.text}</span><button className="icon-button" aria-label="Bildirimi kapat" onClick={() => setNotice(null)}><Icon name="close" /></button></div>}
      {apiError && <div className="connection-banner" role="alert"><span className="connection-dot"/><div><strong>Yerel API’ye ulaşılamıyor</strong><p>{apiError} Kabul maddeleri ve sistem ölçümleri ancak API yanıt verdiğinde gösterilir; kaydedilmiş son değerler güncelmiş gibi sunulmaz.</p></div><button className="button secondary small" onClick={() => void refresh()}><Icon name="refresh"/>Yeniden dene</button></div>}
      {page === 'overview' && <OverviewPage overview={overview} runs={runs} checks={checks} apiError={apiError} onOpenItem={setSelected} onGo={setPage} onWatch={watchRun} watchId={watchId} setWatchId={setWatchId} busy={busy} onRunClick={id => { setRunId(id); setRunDetail(runs.find(run => run.run_id === id) ?? null); setReport(null); }} onNewRun={() => setShowForm(true)} />}
      {page === 'acceptance' && <section className="panel acceptance-page"><div className="panel-heading"><div><h2>Kabul maddeleri</h2><p>{overview?.acceptance.source ?? 'Kaynak bilgisi API yanıtından alınır.'}</p></div><select aria-label="Duruma göre filtrele" value={filter} onChange={event => setFilter(event.target.value as typeof filter)}><option value="all">Tüm durumlar</option><option value="passed">Geçti</option><option value="partial">Kısmi</option><option value="open">Açık</option></select></div>
        {!overview && <EmptyState title="Kabul verisi yok" text="Gerçek kabul maddeleri için yerel API bağlantısı gerekli."/>}
        {overview && <><AcceptanceCounts overview={overview}/><div className="acceptance-list">{filteredItems.map(item => <button className="acceptance-row" key={item.id} onClick={() => setSelected(item)}><span className="item-id">{item.id}</span><span className="item-title">{item.title}</span><StatusPill status={item.status}/><span className="row-chevron">›</span></button>)}</div><div className="source-line">Kaynak: {overview.acceptance.source} · SHA-256 {overview.acceptance.source_sha256}</div></>}
      </section>}
      {page === 'experiments' && <ExperimentsPage overview={overview} runs={runs} apiError={apiError} busy={busy} onNew={() => setShowForm(true)} onWatch={watchRun} watchId={watchId} setWatchId={setWatchId} onSelect={id => { setRunId(id); setRunDetail(runs.find(run => run.run_id === id) ?? null); setReport(null); }} onReport={openReport} onStop={stopRun} />}
      {page === 'modes' && <OperatingModesPage connected={!!overview?.lab.connected} onStarted={() => { void refresh(); setPage('experiments'); }} />}
      {page === 'system' && <SystemPage overview={overview} apiError={apiError} checks={checks} onStartCheck={startCheck} busy={busy} />}
      <footer className="page-footer"><span>{overview ? `Ölçüm zamanı: ${fmtTime(overview.generated_at)}` : 'Canlı veri bekleniyor'}</span><button className="button secondary small" onClick={() => void refresh()}><Icon name="refresh"/>Yenile</button></footer>
    </main>
    {selected && <AcceptanceDialog item={selected} onClose={() => { setSelected(null); setEvidence(null); }} onEvidence={openEvidence} evidence={evidence}/ >}
    {showForm && <RunDialog overview={overview} form={form} setForm={setForm} error={formError} busy={busy} onSubmit={startRun} onClose={() => { setShowForm(false); setFormError(null); }} />}
    {runId && <RunDrawer id={runId} detail={runDetail} report={report} onClose={() => { setRunId(null); setRunDetail(null); setReport(null); }} onStop={stopRun} busy={busy} />}
  </div>;
}
function errorText(error: unknown) { return error instanceof ApiError ? error.message : error instanceof Error ? error.message : 'Beklenmeyen hata.'; }
function NavButton({ name, label, active, onClick }: { name: string; label: string; active: boolean; onClick: () => void }) { return <button className={`nav-button ${active ? 'active' : ''}`} aria-current={active ? 'page' : undefined} onClick={onClick}><Icon name={name}/><span>{label}</span></button>; }
function EmptyState({ title, text }: { title: string; text: string }) { return <div className="empty-state"><strong>{title}</strong><span>{text}</span></div>; }
function StatusPill({ status }: { status: AcceptanceStatus }) { return <span className={`status-pill ${status}`}>{statusText[status]}</span>; }
function AcceptanceCounts({ overview }: { overview: Overview }) {
  const a = overview.acceptance;
  const segments = [...Array.from({ length: a.passed }, () => 'passed'), ...Array.from({ length: a.partial }, () => 'partial'), ...Array.from({ length: a.open }, () => 'open')];
  return <div className="acceptance-counts"><div className="count-primary"><strong>{a.passed} / {a.total}</strong><span>madde geçti</span></div><div className="count-visual"><div className="segments" data-testid="acceptance-progress" role="img" aria-label={`${a.passed} geçti, ${a.partial} kısmi, ${a.open} açık`} style={{ '--segment-count': Math.max(1, a.total) } as React.CSSProperties}>{segments.map((status, index) => <span className={`segment ${status}`} key={`${status}-${index}`} />)}</div><div className="legend"><span><i className="passed"/>{a.passed} Geçti</span><span><i className="partial"/>{a.partial} Kısmi</span><span><i className="open"/>{a.open} Açık</span></div><p>Bu oran kabul maddelerini gösterir; işin tamamlanma yüzdesi değildir.</p></div></div>;
}
function OverviewPage({ overview, runs, checks, apiError, onOpenItem, onGo, onWatch, watchId, setWatchId, busy, onRunClick, onNewRun }: {
  overview: Overview | null; runs: Run[]; checks: Check[]; apiError: string | null; onOpenItem: (item: AcceptanceItem) => void;
  onGo: (page: Page) => void; onWatch: (event: React.FormEvent) => void; watchId: string; setWatchId: (value: string) => void;
  busy: boolean; onRunClick: (id: string) => void; onNewRun: () => void;
}) {
  const priorityIds = ['M0.13', 'M0.10', 'M0.AOS.7', 'M0.14'];
  const priorities = priorityIds.map(id => overview?.acceptance.items.find(item => item.id === id)).filter((item): item is AcceptanceItem => !!item);
  return <div className="overview-content">
    <section className="panel acceptance-summary"><div className="panel-title-row"><div><h2>M0 kabul durumu</h2></div></div>
      {overview ? <AcceptanceCounts overview={overview}/> : <EmptyState title="Kabul verisi henüz alınmadı" text={apiError ? 'Bağlantı geri geldiğinde güncel sayımlar burada görünür.' : 'API’den veri bekleniyor.'}/ >}
    </section>
    <div className="dashboard-grid">
    <section className="panel next-work"><div className="panel-heading"><h2>Sıradaki işler</h2></div>
        {priorities.length ? <><div className="priority-table" data-testid="gate-list"><div className="table-head"><span>Kabul maddesi</span><span>Durum</span><span>Kanıt</span></div>{priorities.map(item => <div className="priority-row" key={item.id}><span className="item-title">{({ 'M0.13': 'Yerel Qwen S1 / S2', 'M0.10': 'Holdout ve gizlilik', 'M0.AOS.7': 'AOS ile birlikte çalışma', 'M0.14': 'QLoRA donanım ölçümü' } as Record<string, string>)[item.id]}</span><StatusPill status={item.status}/><button className="button secondary small" onClick={() => onOpenItem(item)}>İncele</button></div>)}</div><button className="text-link" onClick={() => onGo('acceptance')}>{overview?.acceptance.total} maddenin tümünü gör <Icon name="arrow"/></button></> : <EmptyState title={overview ? 'Öncelikli kabul maddesi yok' : 'Gerçek kabul verisi bekleniyor'} text={overview ? 'Öncelik listesinde kayıtlı madde bulunamadı.' : 'Konsol örnek veya tahmini maddeler göstermez.'}/>}
      </section>
      <SystemSummary overview={overview}/>
    </div>
    <section className="panel experiment-panel" data-testid="run-list"><div className="panel-heading"><h2>Deneyler</h2><button className="button outline" onClick={onNewRun} disabled={!canRunAnySuite(overview?.lab)} title={!overview?.lab.connected ? reasonText(overview?.lab.reason) ?? 'Lab API bağlı değil' : undefined}>Yeni deney</button></div>
      {!runs.length ? <div className="run-empty"><strong>{apiError ? 'Koşu listesi alınamadı' : 'Henüz bağlı bir deney yok'}</strong><span>{apiError ? 'API bağlantısı düzeldiğinde kayıtlı koşular yüklenir.' : reasonText(overview?.lab.reason) || 'Bir koşu kimliği ekleyin veya kayıtlı bir suite ile başlatın.'}</span></div> : <div className="run-list">{runs.slice(0, 5).map(run => <RunRow key={run.run_id} run={run} onSelect={onRunClick}/>)}</div>}
      <form className="watch-form" onSubmit={onWatch}><label htmlFor="watch-id">Koşu kimliğini izle</label><input id="watch-id" value={watchId} onChange={event => setWatchId(event.target.value)} placeholder="Koşu kimliği (UUID)" inputMode="text" autoComplete="off"/><button className="button primary" disabled={busy || !watchId.trim()} type="submit">İzle</button></form>
      <div className="panel-footer"><span>Kaynak: {overview?.acceptance.source ?? 'API yanıtı bekleniyor'}</span>{checks.find(check => check.state === 'running' || check.state === 'queued') && <span className="check-live">CPU kontrolü {readableState(checks.find(check => check.state === 'running' || check.state === 'queued')!.state).toLocaleLowerCase('tr-TR')}</span>}</div>
    </section>
  </div>;
}
function SystemSummary({ overview }: { overview: Overview | null }) {
  const m = overview?.system.memory; const gpu = overview?.system.gpu;
  const rows = [
    ['RAM', m ? `${fmtBytes(m.available_bytes)} kullanılabilir / ${fmtBytes(m.total_bytes)} toplam` : 'Canlı ölçüm bekleniyor', m ? 'ready' : 'unknown'],
    ['GPU', gpu?.available ? `${gpu.name ?? 'GPU'} · ${gpu.used_mib ?? '—'} / ${gpu.total_mib ?? '—'} MiB` : gpu?.reason ?? 'Canlı ölçüm bekleniyor', gpu?.available ? 'ready' : 'unknown'],
    ['Lab API', overview?.lab.connected ? 'Bağlı' : reasonText(overview?.lab.reason) ?? 'Bağlı değil', overview?.lab.connected ? 'ready' : 'offline'],
  ] as const;
  return <section className="panel system-summary"><div className="panel-heading"><h2>Yerel sistem</h2></div>{rows.map(([label, value, state]) => <div className="system-row" key={label}><strong>{label}</strong><span className={`dot ${state}`}/><span>{value}</span></div>)}{overview && !overview.lab.model_runs_enabled && <div className="warning-note"><span className="warning-mark">!</span>GPU denemeleri AOS koordinasyonunu bekliyor.</div>}</section>;
}
function RunRow({ run, onSelect }: { run: Run; onSelect: (id: string) => void }) { return <button className="run-row" onClick={() => onSelect(run.run_id)}><span className={`run-state ${activeRunStates.has(run.state) ? 'active' : ''}`}>{purposeText(run.purpose)} · {readableState(run.state)}{run.stale || run.unavailable ? ' · Eski durum' : ''}</span><code>{run.run_id}</code><span>{fmtTime(run.updated_at ?? run.created_at)}</span><span className="row-chevron">›</span></button>; }
function SystemPage({ overview, apiError, checks, onStartCheck, busy }: { overview: Overview | null; apiError: string | null; checks: Check[]; onStartCheck: () => void; busy: boolean }) {
  return <div className="page-stack"><section className="panel"><div className="panel-heading"><div><h2>Sistem durumu</h2><p>Yalnız API’nin son canlı ölçümleri</p></div><span className={`live-label ${apiError ? 'offline' : overview ? 'online' : ''}`}><i/>{apiError ? 'Bağlı değil' : overview ? 'Canlı' : 'Bekleniyor'}</span></div>{!overview ? <EmptyState title="Sistem ölçümü yok" text={apiError || 'Yerel API yanıtı bekleniyor.'}/> : <div className="metrics-grid"><Metric label="Bellek" value={fmtBytes(overview.system.memory.total_bytes)} detail={`${fmtBytes(overview.system.memory.available_bytes)} kullanılabilir`}/><Metric label="Disk" value={fmtBytes(overview.system.disk.total_bytes)} detail={`${fmtBytes(overview.system.disk.free_bytes)} boş`}/><Metric label="CPU" value={`${overview.system.cpu.logical_count} mantıksal çekirdek`} detail={overview.system.cpu.load_1m == null ? 'Yük ölçümü yok' : `1 dk yük ortalaması ${overview.system.cpu.load_1m.toFixed(2)}`}/><Metric label="GPU" value={overview.system.gpu.available ? overview.system.gpu.name ?? 'Kullanılabilir' : 'Kullanılamıyor'} detail={overview.system.gpu.available ? `${overview.system.gpu.used_mib ?? '—'} / ${overview.system.gpu.total_mib ?? '—'} MiB · ${overview.system.gpu.utilization_percent ?? '—'}%` : overview.system.gpu.reason || 'Ölçüm yok'}/></div>}</section>
    <section className="panel"><div className="panel-heading"><div><h2>CPU kontrolü</h2><p>İzinli hızlı kalite komutu; kabul sayısını değiştirmez.</p></div><button className="button primary" onClick={onStartCheck} disabled={busy || checks.some(item => item.state === 'queued' || item.state === 'running')}><Icon name="check"/>Kontrolü çalıştır</button></div>{checks.length ? <div className="check-history">{checks.map((check, index) => <details className="check-entry" key={check.id} data-testid={index === 0 ? 'check-result' : undefined} open={index === 0}><summary><span className={`status-pill ${check.state === 'passed' ? 'passed' : check.state === 'failed' ? 'open' : 'partial'}`}>{readableState(check.state)}</span><code>{check.id}</code><span>{fmtTime(check.started_at)}</span><span>Çıkış kodu: {check.exit_code ?? '—'}</span></summary><div className="check-result"><strong>{check.summary || 'Kontrol özeti yok'}</strong><pre>{check.output || 'Kontrol çıktısı yok.'}</pre></div></details>)}</div> : <EmptyState title="Henüz kontrol çalıştırılmadı" text="Çalıştırıldığında gerçek süreç sonucu ve sınırlı çıktısı burada görünür."/>}</section>
    </div>;
}
function Metric({ label, value, detail }: { label: string; value: string | null; detail: string }) { return <div className="metric"><span>{label}</span><strong>{value ?? 'Ölçüm yok'}</strong><small>{detail}</small></div>; }
function ExperimentsPage({ overview, runs, apiError, busy, onNew, onWatch, watchId, setWatchId, onSelect, onReport, onStop }: {
  overview: Overview | null; runs: Run[]; apiError: string | null; busy: boolean; onNew: () => void;
  onWatch: (event: React.FormEvent) => void; watchId: string; setWatchId: (value: string) => void;
  onSelect: (id: string) => void; onReport: (id: string) => void; onStop: (id: string) => void;
}) {
  return <div className="page-stack"><section className="panel"><div className="panel-heading"><div><h2>Deneyler</h2><p>Yerel Lab API üzerinden doğrulanıp izlenen koşular</p></div><button className="button primary" onClick={onNew} disabled={!canRunAnySuite(overview?.lab)} title={!overview?.lab.connected ? reasonText(overview?.lab.reason) ?? 'Lab API bağlı değil' : undefined}>Yeni deney başlat</button></div>
    {!overview?.lab.connected && <div className="connection-inline"><strong>Deney başlatılamıyor</strong><p>{apiError || reasonText(overview?.lab.reason) || 'Lab API bağlantısı kurulmadı. Koşu başlatma ve izleme gerçek API yanıtı gerektirir.'}</p></div>}
    {overview?.lab.connected && !canRunAnySuite(overview.lab) && <div className="connection-inline"><strong>Model deneyleri kapalı</strong><p>{reasonText(overview.lab.reason) || 'Kayıtlı bir suite şu an çalıştırılabilir değil.'}</p></div>}
    {runs.length ? <div className="runs-table"><div className="table-head"><span>Durum</span><span>Koşu UUID</span><span>Son güncelleme</span><span>Eylemler</span></div>{runs.map(run => <div className="runs-table-row" key={run.run_id}><span className={`run-state ${activeRunStates.has(run.state) ? 'active' : ''}`}>{purposeText(run.purpose)} · {readableState(run.state)}{run.stale || run.unavailable ? ' · Ulaşılamıyor' : ''}</span><button className="uuid-button" onClick={() => onSelect(run.run_id)}>{run.run_id}</button><span>{fmtTime(run.updated_at ?? run.created_at)}</span><span className="row-actions"><button className="button secondary tiny" onClick={() => onSelect(run.run_id)}>Durum</button><button className="button secondary tiny" onClick={() => onReport(run.run_id)} disabled={!run.report_sha256}>Rapor</button><button className="button secondary tiny danger-text" onClick={() => onStop(run.run_id)} disabled={busy || !stoppableRunStates.has(run.state)}>{run.state === 'stop_requested' ? 'Durdurma bekleniyor…' : 'Durdur'}</button></span></div>)}</div> : <EmptyState title={apiError ? 'Koşular yüklenemedi' : 'Henüz izlenen koşu yok'} text={apiError || 'Yeni deney başlatın veya mevcut Lab koşusunu UUID ile doğrulayıp izlemeye ekleyin.'}/>}
  </section><section className="panel watch-panel"><h2>Mevcut koşuyu izle</h2><p>Koşu yalnız gerçek API doğrulamasından sonra listeye eklenir.</p><form className="watch-form left" onSubmit={onWatch}><input aria-label="Koşu UUID’si" value={watchId} onChange={event => setWatchId(event.target.value)} placeholder="Koşu kimliği (UUID)" autoComplete="off"/><button className="button primary" type="submit" disabled={busy || !watchId.trim()}>İzle</button></form></section></div>;
}
function AcceptanceDialog({ item, onClose, onEvidence, evidence }: { item: AcceptanceItem; onClose: () => void; onEvidence: (ref: AcceptanceItem['evidence'][number]) => void; evidence: { ref: AcceptanceItem['evidence'][number]; data?: EvidenceResponse; error?: string } | null }) {
  useEscape(onClose);
  return <div className="overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><section className="dialog" role="dialog" aria-modal="true" aria-labelledby="acceptance-dialog-title" tabIndex={-1}><div className="dialog-header"><div><span className="eyebrow">{item.id}</span><h2 id="acceptance-dialog-title">{item.title}</h2></div><button className="icon-button" onClick={onClose} aria-label="Kapat"><Icon name="close"/></button></div><div className="dialog-body"><StatusPill status={item.status}/><p className="detail-copy">{item.detail}</p><h3>Kanıtlar</h3>{item.evidence.length ? item.evidence.map(ref => <button className="evidence-link" key={ref.url} onClick={() => onEvidence(ref)}><Icon name="file"/><span>{ref.name}</span><small>{ref.kind}</small></button>) : <p className="muted">Bu madde için kayıtlı kanıt bağlantısı yok.</p>}{evidence && <div className="evidence-content"><div className="evidence-title"><strong>{evidence.ref.name}</strong><button className="icon-button" onClick={() => onEvidence(evidence.ref)} aria-label="Kanıtı yeniden yükle"><Icon name="refresh"/></button></div>{evidence.error ? <p className="error-text">{evidence.error}</p> : evidence.data ? <>{evidence.data.truncated && <p className="muted">İçerik 256 KiB sınırında kısaltıldı.</p>}{evidence.data.kind === 'html' ? <iframe className="evidence-frame" title={evidence.data.name} sandbox="" srcDoc={evidence.data.content}/> : <><pre>{evidence.data.content}</pre><small className="evidence-hash">SHA-256 {evidence.data.sha256}</small></>}</> : <p className="muted">Kanıt yükleniyor…</p>}</div>}</div></section></div>;
}
function RunDialog({ overview, form, setForm, error, busy, onSubmit, onClose }: {
  overview: Overview | null; form: RunForm;
  setForm: (value: RunForm) => void;
  error: string | null; busy: boolean; onSubmit: (event: React.FormEvent) => void; onClose: () => void;
}) {
  useEscape(onClose);
  const suite = overview?.lab.suites.find(item => item.suite_id === form.suite);
  const disabled = busy || !canRunSuite(overview?.lab, suite, form.purpose);
  return <div className="overlay" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}><section className="dialog form-dialog" role="dialog" aria-modal="true" aria-labelledby="run-dialog-title" tabIndex={-1}><div className="dialog-header"><div><span className="eyebrow">LAB API</span><h2 id="run-dialog-title">Yeni deney</h2></div><button className="icon-button" onClick={onClose} aria-label="Kapat"><Icon name="close"/></button></div><form className="dialog-body run-form" onSubmit={onSubmit}><p className="muted">Yalnız kayıtlı suite’ler ve tanımlı bütçe sınırları kullanılabilir.</p><label>İşlem<select value={form.purpose} onChange={event => setForm({ ...form, purpose: event.target.value as RunForm['purpose'] })}><option value="baseline">CPU baseline · model kullanmaz</option><option value="research">Araştırma · kayıtlı sağlayıcı</option></select></label><p className="muted">{form.purpose === 'baseline' ? 'Kayıtlı veri üzerinde CPU baseline ölçümü; öneri ve model token bütçesi sıfırdır.' : suite?.provider === 'fake-json' ? 'Fixture araştırması: hazır öneriler kullanır, gerçek model kanıtı değildir.' : 'Yerel model ile araştırma; model çalıştırma izni gerekir.'}</p><label>Suite<select required value={form.suite} onChange={event => { const nextSuite = overview?.lab.suites.find(item => item.suite_id === event.target.value); const cap = Math.min(35, nextSuite?.proposal_limit ?? 35); setForm({ ...form, suite: event.target.value, experiments: Math.min(form.experiments, cap) }); }} disabled={!overview?.lab.suites.length}><option value="">Suite seçin</option>{overview?.lab.suites.map(item => <option key={item.suite_id} value={item.suite_id}>{item.suite_id} · {item.track} · {item.provider}</option>)}</select></label>{suite && <div className="suite-meta"><span>İz: {suite.track}</span><span>Program: {suite.program_version}</span><span>Proposal tavanı: {suite.proposal_limit}</span></div>}{form.purpose === 'research' && <label>Deney adedi<input type="number" min="1" max={Math.min(35, suite?.proposal_limit ?? 35)} value={form.experiments} onChange={event => setForm({ ...form, experiments: Number(event.target.value) })} required/></label>}<label>Süre sınırı (saniye)<input type="number" min="1" max={Math.min(14400, suite?.max_wall_seconds ?? 14400)} value={form.wall_seconds} onChange={event => setForm({ ...form, wall_seconds: Number(event.target.value) })} required/></label>{form.purpose === 'research' && <label>Model token sınırı<input type="number" min="0" max={Math.min(350000, suite?.max_model_tokens ?? 350000)} value={form.model_tokens} onChange={event => setForm({ ...form, model_tokens: Number(event.target.value) })} required/></label>}{!overview?.lab.connected && <div className="connection-inline"><strong>Başlatma kullanılamıyor</strong><p>{reasonText(overview?.lab.reason) || 'Lab API bağlı değil.'}</p></div>}{overview?.lab.connected && suite && !canRunSuite(overview.lab, suite, form.purpose) && <div className="connection-inline"><strong>Bu suite kullanılamıyor</strong><p>{reasonText(overview.lab.reason) || 'Seçili suite için model çalıştırma etkin değil.'}</p></div>}{error && <div className="field-error" role="alert">{error}</div>}<div className="dialog-actions"><button className="button secondary" type="button" onClick={onClose} disabled={busy}>Vazgeç</button><button className="button primary" type="submit" disabled={disabled}>{busy ? 'Başlatılıyor…' : `${purposeText(form.purpose)} başlat`}</button></div></form></section></div>;
}
function RunDrawer({ id, detail, report, onClose, onStop, busy }: { id: string; detail: Run | null; report: unknown; onClose: () => void; onStop: (id: string) => void; busy: boolean }) {
  useEscape(onClose);
  return <div className="overlay drawer-overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><aside className="run-drawer" role="dialog" aria-modal="true" aria-labelledby="run-drawer-title" tabIndex={-1}><div className="dialog-header"><div><span className="eyebrow">KOŞU DURUMU</span><h2 id="run-drawer-title">Deney ayrıntısı</h2></div><button className="icon-button" aria-label="Kapat" onClick={onClose}><Icon name="close"/></button></div><div className="dialog-body"><span className={`run-state ${detail && activeRunStates.has(detail.state) ? 'active' : ''}`}>{detail ? readableState(detail.state) : 'Durum yükleniyor…'}</span><label className="field-label">Koşu UUID</label><code className="uuid-box">{id}</code>{detail && <dl className="run-meta"><dt>İşlem</dt><dd>{purposeText(detail.purpose)}</dd><dt>Oluşturulma</dt><dd>{fmtTime(detail.created_at)}</dd><dt>Güncelleme</dt><dd>{fmtTime(detail.updated_at)}</dd><dt>Kaynak</dt><dd>{detail.origin ?? '—'}</dd><dt>Durdurma isteği</dt><dd>{detail.stop_requested ? 'İletildi' : 'Yok'}</dd><dt>Rapor SHA-256</dt><dd>{detail.report_sha256 ?? 'Henüz yok'}</dd></dl>}{detail && activeRunStates.has(detail.state) && <button className="button danger" onClick={() => onStop(id)} disabled={busy || !stoppableRunStates.has(detail.state)}>{detail.state === 'stop_requested' ? 'Durdurma bekleniyor…' : 'Deneyi durdur'}</button>}{report !== null && <div className="report-view"><h3>Doğrulanmış rapor</h3><button className="button secondary" onClick={() => { const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' })); const link = document.createElement('a'); link.href = url; link.download = `lab-report-${id}.json`; link.click(); URL.revokeObjectURL(url); }}>Rapor JSON indir</button><ModeDiagnostics runId={id} report={report}/><pre>{JSON.stringify(report, null, 2)}</pre></div>}</div></aside></div>;
}
function useEscape(onClose: () => void) {
  const closeRef = useRef(onClose);
  useEffect(() => { closeRef.current = onClose; }, [onClose]);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = document.querySelector<HTMLElement>('[role=\"dialog\"]');
    const focusable = () => dialog?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex=\"0\"]');
    const first = focusable()?.[0];
    (first ?? dialog)?.focus();
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { closeRef.current(); return; }
      if (event.key !== 'Tab' || !dialog) return;
      const items = Array.from(focusable() ?? []);
      if (!items.length) { event.preventDefault(); dialog.focus(); return; }
      const firstItem = items[0]; const lastItem = items[items.length - 1];
      if (event.shiftKey && document.activeElement === firstItem) { event.preventDefault(); lastItem.focus(); }
      else if (!event.shiftKey && document.activeElement === lastItem) { event.preventDefault(); firstItem.focus(); }
    };
    window.addEventListener('keydown', handler);
    return () => { window.removeEventListener('keydown', handler); previous?.focus(); };
  }, []);
}

export default App;
