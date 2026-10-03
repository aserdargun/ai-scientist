import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError } from './api';
import { t, locale, useI18n } from './i18n';
import { useTheme } from './theme';
import { requestId } from './requestId';
import { ModeStreamProgress } from './ModeStreamProgress';
import { ModeDiagnostics } from './ModeDiagnostics';
import { OperatingModesPage } from './OperatingModesPage';
import { AgentWorkspace } from './AgentWorkspace';
import type { AcceptanceItem, AcceptanceStatus, Check, EvidenceResponse, FieldIntent, Overview, PriorExperience, Run, Suite } from './types';

type RunForm = { purpose: 'baseline' | 'research'; suite: string; experiments: number; wall_seconds: number; model_tokens: number };
const purposeText = (purpose: Run['purpose']) => purpose === 'baseline' ? 'CPU baseline' : purpose === 'mode-stream' ? t("CPU OMR stream · unscored", "CPU OMR akışı · puanlanmaz") : purpose === 'mode-grid' ? t("Parameter sweep · 0 tokens", "Parametre taraması · 0 token") : purpose === 'research' ? t("Research", "Araştırma") : t("Unknown type", "Tür bilinmiyor");
type Page = 'agent' | 'overview' | 'acceptance' | 'experiments' | 'system' | 'modes';
type Notice = { kind: 'error' | 'success'; text: string } | null;
const statusText = (): Record<AcceptanceStatus, string> => ({ passed: t("Passed", "Geçti"), partial: t("Partial", "Kısmi"), open: t("Open", "Açık") });
const stateText = (): Record<string, string> => ({ queued: t("Queued", "Kuyrukta"), running: t("Running", "Çalışıyor"), stop_requested: t("Stop requested", "Durdurma bekleniyor"), passed: t("Passed", "Başarılı"), failed: t("Failed", "Başarısız"), completed: t("Completed", "Tamamlandı"), stopped: t("Stopped", "Durduruldu"), cancelled: t("Cancelled", "İptal edildi"), error: t("Error", "Hata") });
const activeRunStates = new Set(['queued', 'running', 'stop_requested']);
const stoppableRunStates = new Set(['queued', 'running']);
const readableState = (state: string) => stateText()[state] ?? state;
const reasonText = (reason: string | null | undefined) => reason?.toLocaleLowerCase('en-US').includes('lab api is not configured') ? t("The Lab API is not configured yet.", "Lab API henüz yapılandırılmadı.") : reason;
function canRunSuite(lab: Overview['lab'] | undefined, suite: Suite | undefined, purpose: RunForm['purpose'] = 'research') {
  return !!lab?.connected && !!suite && (purpose === 'baseline' || suite.provider !== 'local-qwen' || lab.model_runs_enabled);
}
function canRunAnySuite(lab: Overview['lab'] | undefined) {
  return !!lab?.suites.some(suite => canRunSuite(lab, suite, 'baseline'));
}
const fmtNumber = (value: number, digits = 0) => value.toLocaleString(locale(), { minimumFractionDigits: digits, maximumFractionDigits: digits });
const fmtMaybeNumber = (value: number | null | undefined) => value == null ? "—" : fmtNumber(value);
const fmtBytes = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? null : value >= 1024 ** 3 ? `${fmtNumber(value / 1024 ** 3, 1)} GiB` : `${fmtNumber(value / 1024 ** 2)} MiB`;
const fmtTime = (value: string | null | undefined) => value ? new Date(value).toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'short' }) : '—';
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function Icon({ name }: { name: string }) {
  const paths: Record<string, React.ReactNode> = {
    agent: <><rect x="4" y="6" width="16" height="14" rx="3"/><path d="M12 2v4M8 12h.01M16 12h.01M8 16h8M2 10h2M20 10h2"/></>,
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
  const { language, setLanguage } = useI18n();
  const { theme, setTheme } = useTheme();
  const [page, setPage] = useState<Page>('agent');
  const [modesMounted, setModesMounted] = useState(false);
  const [experimentKind, setExperimentKind] = useState<'grid' | 'agent'>('agent');
  const openLocalAgent = () => { setExperimentKind('agent'); setPage('modes'); };
  const openManualGrid = () => { setExperimentKind('grid'); setPage('modes'); };
  const [fieldIntentDraft, setFieldIntentDraft] = useState<FieldIntent>({ asset_id: '', goal_kind: 'digital_twin', objective: '' });
  const [fieldIntent, setFieldIntent] = useState<FieldIntent | null>(null);
  const clearFieldIntent = () => { setFieldIntent(null); setFieldIntentDraft({ asset_id: '', goal_kind: 'digital_twin', objective: '' }); };
  const [priorExperience, setPriorExperience] = useState<PriorExperience | null>(null);
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

  useEffect(() => { if (page === 'modes') setModesMounted(true); }, [page]);
  const currentPrior = priorExperience && overview?.lab.connected && runs.some(run => !run.stale && !run.unavailable
    && ['completed', 'stopped', 'failed'].includes(run.state) && run.run_id === priorExperience.source_run_id
    && run.report_sha256 === priorExperience.source_report_sha256) ? priorExperience : null;
  useEffect(() => { if (priorExperience && !currentPrior) setPriorExperience(null); }, [priorExperience, currentPrior]);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const results = await Promise.allSettled([api.overview(signal), api.runs(signal), api.checks(signal)]);
    if (signal?.aborted) return;
    const overviewResult = results[0];
    if (overviewResult.status === 'fulfilled') {
      setOverview(overviewResult.value); setApiError(null);
      setForm(current => current.suite || !overviewResult.value.lab.suites.length ? current : { ...current, suite: overviewResult.value.lab.suites[0].suite_id });
    } else if (!(overviewResult.reason instanceof DOMException && overviewResult.reason.name === 'AbortError')) {
      setOverview(null);
      setApiError(overviewResult.reason instanceof Error ? overviewResult.reason.message : t("The local API did not respond.", "Yerel API yanıt vermedi."));
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
      setNotice({ kind: 'success', text: t("CPU check started. Results will appear on the System page.", "CPU kontrolü başlatıldı. Sonuç Sistem ekranında görünecek.") });
      await refresh();
    } catch (error) { setNotice({ kind: 'error', text: errorText(error) }); }
    finally { setBusy(false); }
  }
  async function watchRun(event: React.FormEvent) {
    event.preventDefault(); setNotice(null);
    if (!uuidPattern.test(watchId.trim())) { setNotice({ kind: 'error', text: t("Enter a valid run UUID.", "Geçerli bir koşu UUID’si girin.") }); return; }
    setBusy(true);
    try {
      const tracked = await api.watchRun(watchId.trim());
      setRunId(tracked.run_id); setRunDetail(tracked); setWatchId(''); await refresh();
      setNotice({ kind: 'success', text: t(`Run ${tracked.run_id} was verified and added to tracking.`, `Koşu ${tracked.run_id} doğrulanıp izlemeye eklendi.`) });
    } catch (error) { setNotice({ kind: 'error', text: errorText(error) }); }
    finally { setBusy(false); }
  }
  async function startRun(event: React.FormEvent) {
    event.preventDefault(); setFormError(null);
    if (!overview?.lab.connected) { setFormError(reasonText(overview?.lab.reason) || t("The Lab API is disconnected; a new experiment cannot start.", "Lab API bağlı değil; yeni deney başlatılamıyor.")); return; }
    if (!selectedSuite || !canRunSuite(overview.lab, selectedSuite, form.purpose)) { setFormError(reasonText(overview.lab.reason) || t("Model runs are disabled for the selected suite.", "Seçili suite için model çalıştırma etkin değil.")); return; }
    const withoutKey = {
      track: selectedSuite.track, suite: selectedSuite.suite_id,
      program_version: selectedSuite.program_version,
      budget: { experiments: Number(form.experiments), wall_seconds: Number(form.wall_seconds), model_tokens: Number(form.model_tokens) },
    };
    const serializedRequest = JSON.stringify({ purpose: form.purpose, ...withoutKey });
    let retry: { request: string; key: string } | null = null;
    try { retry = JSON.parse(window.sessionStorage.getItem('lab-console-start-retry') || 'null') as { request: string; key: string } | null; } catch { retry = null; }
    const key = retry?.request === serializedRequest ? retry.key : requestId();
    window.sessionStorage.setItem('lab-console-start-retry', JSON.stringify({ request: serializedRequest, key }));
    setBusy(true);
    try {
      const created = form.purpose === 'baseline'
        ? await api.startBaseline({ suite: selectedSuite.suite_id, program_version: selectedSuite.program_version, idempotency_key: key, budget: { experiments: 0, wall_seconds: Number(form.wall_seconds), model_tokens: 0 } })
        : await api.startRun({ ...withoutKey, idempotency_key: key });
      window.sessionStorage.removeItem('lab-console-start-retry');
      setRunId(created.run_id); setRunDetail({ run_id: created.run_id, state: created.state, purpose: form.purpose });
      setShowForm(false); await refresh();
      setNotice({ kind: 'success', text: `${purposeText(form.purpose)} ${created.run_id} ${created.reused ? t("was recovered from the previous request", "önceki isteğin sonucu olarak bulundu") : t("started", "başlatıldı")}.` });
    } catch (error) { setFormError(errorText(error)); }
    finally { setBusy(false); }
  }
  async function stopRun(id: string) {
    setBusy(true); setNotice(null);
    try { setRunDetail(await api.stopRun(id)); await refresh(); setNotice({ kind: 'success', text: t(`Stop requested for ${id}.`, `Durdurma isteği ${id} için iletildi.`) }); }
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
    <header className="app-topbar">
      <div className="brand"><div className="brand-name">AI Scientist</div><div className="brand-subtitle">{t("Local research", "Yerel araştırma")}</div></div>
      <div className="language-switch" role="group" aria-label="Language / Dil"><button type="button" lang="en" aria-pressed={language === 'en'} onClick={() => setLanguage('en')}>English</button><button type="button" lang="tr" aria-pressed={language === 'tr'} onClick={() => setLanguage('tr')}>Türkçe</button></div>
      <div className="theme-switch" role="group" aria-label="Theme / Tema"><button type="button" data-theme-value="light" aria-pressed={theme === 'light'} onClick={() => setTheme('light')}>{t("Light theme", "Açık tema")}</button><button type="button" data-theme-value="dark" aria-pressed={theme === 'dark'} onClick={() => setTheme('dark')}>{t("Dark theme", "Koyu tema")}</button></div>
    </header>
    <aside className="sidebar">
      <nav className="side-nav" aria-label={t("Main navigation", "Ana gezinme")}>
        <NavButton name="agent" label={t("Agent", "Eylemci")} active={page === 'agent'} onClick={() => setPage('agent')} />
        <NavButton name="home" label={t("Overview", "Genel bakış")} active={page === 'overview'} onClick={() => setPage('overview')} />
        <NavButton name="check" label={t("Acceptance items", "Kabul maddeleri")} active={page === 'acceptance'} onClick={() => setPage('acceptance')} />
        <NavButton name="flask" label={t("Operating modes", "Çalışma modları")} active={page === 'modes'} onClick={() => setPage('modes')} />
        <NavButton name="flask" label={t("Experiments", "Deneyler")} active={page === 'experiments'} onClick={() => setPage('experiments')} />
        <NavButton name="settings" label={t("System", "Sistem")} active={page === 'system'} onClick={() => setPage('system')} />
      </nav>
      <div className="side-footer">{overview ? `v${overview.version}` : t("Waiting for API", "API bekleniyor")}</div>
    </aside>
    <main className="main">
      <header className="page-header">
        <div><h1>{t("Research control center", "Araştırma kontrol merkezi")}</h1><p>{t("Track progress, run experiments and inspect evidence.", "Tamamlanmayı izle, deneyleri çalıştır, kanıtları incele.")}</p></div>
        <div className="header-tools">
        {page === 'system' || page === 'acceptance'
          ? <button className="button primary header-action" onClick={startCheck} disabled={busy || !!activeCheck} title={activeCheck ? t("A CPU check is already running", "CPU kontrolü zaten çalışıyor") : undefined}>{busy && !activeCheck ? t("Starting…", "Başlatılıyor…") : activeCheck ? t("Check running", "Kontrol çalışıyor") : t("Run check", "Kontrolü çalıştır")}</button>
          : <button className="button primary header-action" onClick={openLocalAgent}>{t("Design an experiment with the local agent", "Yerel eylemciyle deney tasarla")}</button>}
      </div></header>
      {notice && <div className={`notice ${notice.kind}`} role="status"><span>{notice.text}</span><button className="icon-button" aria-label={t("Dismiss notification", "Bildirimi kapat")} onClick={() => setNotice(null)}><Icon name="close" /></button></div>}
      {apiError && <div className="connection-banner" role="alert"><span className="connection-dot"/><div><strong>{t("Local API unavailable", "Yerel API’ye ulaşılamıyor")}</strong><p>{apiError} {t("Acceptance items and system measurements appear when the API responds; saved values are not presented as current.", "Kabul maddeleri ve sistem ölçümleri ancak API yanıt verdiğinde gösterilir; kaydedilmiş son değerler güncelmiş gibi sunulmaz.")}</p></div><button className="button secondary small" onClick={() => void refresh()}><Icon name="refresh"/>{t("Retry", "Yeniden dene")}</button></div>}
      {page === 'agent' && <AgentWorkspace fieldIntentDraft={fieldIntentDraft} onFieldIntentDraftChange={intent => { setFieldIntentDraft(intent); setFieldIntent(null); }} onClearFieldIntent={clearFieldIntent} onDesignWithFieldIntent={intent => { setFieldIntentDraft(intent); setFieldIntent(intent); openLocalAgent(); }} onDesignWithHistory={prior => { setPriorExperience(prior); openLocalAgent(); }} overview={overview} runs={runs} onModes={openLocalAgent} onManualGrid={openManualGrid} onExperiments={() => setPage('experiments')} onEvidence={setSelected} onRun={id => { setRunId(id); setRunDetail(runs.find(run => run.run_id === id) ?? null); setReport(null); }} onReport={openReport} onAnomaly={() => { const suite = overview?.lab.suites.find(item => item.track === 'anomaly'); if (suite) setForm(current => ({ ...current, suite: suite.suite_id, purpose: 'baseline' })); setShowForm(true); }}/ >}
      {page === 'overview' && <OverviewPage overview={overview} runs={runs} checks={checks} apiError={apiError} onOpenItem={setSelected} onGo={setPage} onWatch={watchRun} watchId={watchId} setWatchId={setWatchId} busy={busy} onRunClick={id => { setRunId(id); setRunDetail(runs.find(run => run.run_id === id) ?? null); setReport(null); }} onNewRun={() => setShowForm(true)} />}
      {page === 'acceptance' && <section className="panel acceptance-page"><div className="panel-heading"><div><h2>{t("Acceptance items", "Kabul maddeleri")}</h2><p>{overview?.acceptance.source ?? t("Source information comes from the API response.", "Kaynak bilgisi API yanıtından alınır.")}</p></div><select aria-label={t("Filter by status", "Duruma göre filtrele")} value={filter} onChange={event => setFilter(event.target.value as typeof filter)}><option value="all">{t("All statuses", "Tüm durumlar")}</option><option value="passed">{t("Passed", "Geçti")}</option><option value="partial">{t("Partial", "Kısmi")}</option><option value="open">{t("Open", "Açık")}</option></select></div>
        {!overview && <EmptyState title={t("No acceptance data", "Kabul verisi yok")} text={t("A local API connection is required for actual acceptance items.", "Gerçek kabul maddeleri için yerel API bağlantısı gerekli.")}/>}
        {overview && <><AcceptanceCounts overview={overview}/><div className="acceptance-list">{filteredItems.map(item => <button className="acceptance-row" key={item.id} onClick={() => setSelected(item)}><span className="item-id">{item.id}</span><span className="item-title">{item.title}</span><StatusPill status={item.status}/><span className="row-chevron">›</span></button>)}</div><div className="source-line">{t("Source:", "Kaynak:")} {overview.acceptance.source} · SHA-256 {overview.acceptance.source_sha256}</div></>}
      </section>}
      {page === 'experiments' && <ExperimentsPage overview={overview} runs={runs} apiError={apiError} busy={busy} onNew={() => setShowForm(true)} onWatch={watchRun} watchId={watchId} setWatchId={setWatchId} onSelect={id => { setRunId(id); setRunDetail(runs.find(run => run.run_id === id) ?? null); setReport(null); }} onReport={openReport} onStop={stopRun} />}
      {(page === 'modes' || modesMounted) && <div hidden={page !== 'modes'}><OperatingModesPage experimentKind={experimentKind} onExperimentKindChange={setExperimentKind} fieldIntent={fieldIntent} onClearFieldIntent={clearFieldIntent} onEditFieldIntent={() => { setPage('agent'); window.location.hash = 'agent-field-intent'; }} priorExperience={currentPrior} onClearPrior={() => setPriorExperience(null)} connected={!!overview?.lab.connected} modelRunsEnabled={!!overview?.lab.model_runs_enabled} onStarted={() => { setPriorExperience(null); clearFieldIntent(); void refresh(); setPage('experiments'); }} /></div>}
      {page === 'system' && <SystemPage overview={overview} apiError={apiError} checks={checks} onStartCheck={startCheck} busy={busy} />}
      <footer className="page-footer"><span>{overview ? t(`Measured at: ${fmtTime(overview.generated_at)}`, `Ölçüm zamanı: ${fmtTime(overview.generated_at)}`) : t("Waiting for live data", "Canlı veri bekleniyor")}</span><button className="button secondary small" onClick={() => void refresh()}><Icon name="refresh"/>{t("Refresh", "Yenile")}</button></footer>
    </main>
    {selected && <AcceptanceDialog item={selected} onClose={() => { setSelected(null); setEvidence(null); }} onEvidence={openEvidence} evidence={evidence}/ >}
    {showForm && <RunDialog overview={overview} form={form} setForm={setForm} error={formError} busy={busy} onSubmit={startRun} onClose={() => { setShowForm(false); setFormError(null); }} />}
    {runId && <RunDrawer id={runId} detail={runDetail} report={report} onClose={() => { setRunId(null); setRunDetail(null); setReport(null); }} onStop={stopRun} busy={busy} />}
  </div>;
}
function errorText(error: unknown) { return error instanceof ApiError ? error.message : error instanceof Error ? error.message : t("Unexpected error.", "Beklenmeyen hata."); }
function NavButton({ name, label, active, onClick }: { name: string; label: string; active: boolean; onClick: () => void }) { return <button className={`nav-button ${active ? 'active' : ''}`} aria-current={active ? 'page' : undefined} onClick={onClick}><Icon name={name}/><span>{label}</span></button>; }
function EmptyState({ title, text }: { title: string; text: string }) { return <div className="empty-state"><strong>{title}</strong><span>{text}</span></div>; }
function StatusPill({ status }: { status: AcceptanceStatus }) { return <span className={`status-pill ${status}`}>{statusText()[status]}</span>; }
function AcceptanceCounts({ overview }: { overview: Overview }) {
  const a = overview.acceptance;
  const segments = [...Array.from({ length: a.passed }, () => 'passed'), ...Array.from({ length: a.partial }, () => 'partial'), ...Array.from({ length: a.open }, () => 'open')];
  return <div className="acceptance-counts"><div className="count-primary"><strong>{fmtNumber(a.passed)} / {fmtNumber(a.total)}</strong><span>{t("items passed", "madde geçti")}</span></div><div className="count-visual"><div className="segments" data-testid="acceptance-progress" role="img" aria-label={t(`${fmtNumber(a.passed)} passed, ${fmtNumber(a.partial)} partial, ${fmtNumber(a.open)} open`, `${fmtNumber(a.passed)} geçti, ${fmtNumber(a.partial)} kısmi, ${fmtNumber(a.open)} açık`)} style={{ '--segment-count': Math.max(1, a.total) } as React.CSSProperties}>{segments.map((status, index) => <span className={`segment ${status}`} key={`${status}-${index}`} />)}</div><div className="legend"><span><i className="passed"/>{fmtNumber(a.passed)} {t("Passed", "Geçti")}</span><span><i className="partial"/>{fmtNumber(a.partial)} {t("Partial", "Kısmi")}</span><span><i className="open"/>{fmtNumber(a.open)} {t("Open", "Açık")}</span></div><p>{t("This ratio shows acceptance items, not the percentage of work completed.", "Bu oran kabul maddelerini gösterir; işin tamamlanma yüzdesi değildir.")}</p></div></div>;
}
function OverviewPage({ overview, runs, checks, apiError, onOpenItem, onGo, onWatch, watchId, setWatchId, busy, onRunClick, onNewRun }: {
  overview: Overview | null; runs: Run[]; checks: Check[]; apiError: string | null; onOpenItem: (item: AcceptanceItem) => void;
  onGo: (page: Page) => void; onWatch: (event: React.FormEvent) => void; watchId: string; setWatchId: (value: string) => void;
  busy: boolean; onRunClick: (id: string) => void; onNewRun: () => void;
}) {
  const priorityIds = ['M0.13', 'M0.10', 'M0.AOS.7', 'M0.14'];
  const priorities = priorityIds.map(id => overview?.acceptance.items.find(item => item.id === id)).filter((item): item is AcceptanceItem => !!item);
  const recentRuns = [...runs].sort((left, right) => Number(activeRunStates.has(right.state)) - Number(activeRunStates.has(left.state)) || Date.parse(right.updated_at ?? right.created_at ?? '') - Date.parse(left.updated_at ?? left.created_at ?? ''));
  return <div className="overview-content">
    <DevelopmentProgressPanel progress={overview?.development_progress} runs={runs} onRunClick={onRunClick}/>
    <section className="panel acceptance-summary"><div className="panel-title-row"><div><h2>{t("M0 acceptance status", "M0 kabul durumu")}</h2></div></div>
      {overview ? <AcceptanceCounts overview={overview}/> : <EmptyState title={t("Acceptance data not received yet", "Kabul verisi henüz alınmadı")} text={apiError ? t("Current counts will appear when the connection returns.", "Bağlantı geri geldiğinde güncel sayımlar burada görünür.") : t("Waiting for API data.", "API’den veri bekleniyor.")}/ >}
    </section>
    <div className="dashboard-grid">
    <section className="panel next-work"><div className="panel-heading"><h2>{t("Next tasks", "Sıradaki işler")}</h2></div>
        {priorities.length ? <><div className="priority-table" data-testid="gate-list"><div className="table-head"><span>{t("Acceptance item", "Kabul maddesi")}</span><span>{t("Status", "Durum")}</span><span>{t("Evidence", "Kanıt")}</span></div>{priorities.map(item => <div className="priority-row" key={item.id}><span className="item-title">{({ 'M0.13': t("Local Qwen S1 / S2", "Yerel Qwen S1 / S2"), 'M0.10': t("Holdout and privacy", "Holdout ve gizlilik"), 'M0.AOS.7': t("AOS coexistence", "AOS ile birlikte çalışma"), 'M0.14': t("QLoRA hardware measurement", "QLoRA donanım ölçümü") } as Record<string, string>)[item.id]}</span><StatusPill status={item.status}/><button className="button secondary small" onClick={() => onOpenItem(item)}>{t("Inspect", "İncele")}</button></div>)}</div><button className="text-link" onClick={() => onGo('acceptance')}>{overview ? fmtNumber(overview.acceptance.total) : "—"} {t("items: view all", "maddenin tümünü gör")} <Icon name="arrow"/></button></> : <EmptyState title={overview ? t("No priority acceptance items", "Öncelikli kabul maddesi yok") : t("Waiting for actual acceptance data", "Gerçek kabul verisi bekleniyor")} text={overview ? t("No registered items were found in the priority list.", "Öncelik listesinde kayıtlı madde bulunamadı.") : t("The console does not display sample or estimated items.", "Konsol örnek veya tahmini maddeler göstermez.")}/>}
      </section>
      <SystemSummary overview={overview}/>
    </div>
    <section className="panel experiment-panel" data-testid="run-list"><div className="panel-heading"><h2>{t("Experiments", "Deneyler")}</h2><button className="button outline" onClick={onNewRun} disabled={!canRunAnySuite(overview?.lab)} title={!overview?.lab.connected ? reasonText(overview?.lab.reason) ?? t("Lab API disconnected", "Lab API bağlı değil") : undefined}>{t("New experiment", "Yeni deney")}</button></div>
      {!runs.length ? <div className="run-empty"><strong>{apiError ? t("Run list unavailable", "Koşu listesi alınamadı") : t("No experiment connected yet", "Henüz bağlı bir deney yok")}</strong><span>{apiError ? t("Registered runs will load when the API connection returns.", "API bağlantısı düzeldiğinde kayıtlı koşular yüklenir.") : reasonText(overview?.lab.reason) || t("Add a run ID or start a registered suite.", "Bir koşu kimliği ekleyin veya kayıtlı bir suite ile başlatın.")}</span></div> : <div className="run-list">{recentRuns.slice(0, 5).map(run => <RunRow key={run.run_id} run={run} onSelect={onRunClick}/>)}</div>}
      <form className="watch-form" onSubmit={onWatch}><label htmlFor="watch-id">{t("Track a run ID", "Koşu kimliğini izle")}</label><input id="watch-id" value={watchId} onChange={event => setWatchId(event.target.value)} placeholder={t("Run ID (UUID)", "Koşu kimliği (UUID)")} inputMode="text" autoComplete="off"/><button className="button primary" disabled={busy || !watchId.trim()} type="submit">{t("Track", "İzle")}</button></form>
      <div className="panel-footer"><span>{t("Source:", "Kaynak:")} {overview?.acceptance.source ?? t("Waiting for API response", "API yanıtı bekleniyor")}</span>{checks.find(check => check.state === 'running' || check.state === 'queued') && <span className="check-live">{t("CPU check", "CPU kontrolü")} {readableState(checks.find(check => check.state === 'running' || check.state === 'queued')!.state).toLocaleLowerCase(locale())}</span>}</div>
    </section>
  </div>;
}
function DevelopmentProgressPanel({ progress, runs, onRunClick }: { progress: Overview['development_progress']; runs: Run[]; onRunClick: (id: string) => void }) {
  const labels = { pending: t("Pending", "Bekliyor"), running: t("In progress", "Sürüyor"), passed: t("Verified", "Doğrulandı"), blocked: t("Blocked", "Engelli"), quarantined: t("Quarantined", "Karantinada") };
  const activeRun = runs.find(run => run.purpose === 'research' && activeRunStates.has(run.state) && !run.stale && !run.unavailable);
  const progressRunId = progress?.current_work.match(/[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/i)?.[0];
  const baselineCount = progress?.latest_result.match(/Tamamlanan başlangıç CPU ölçümü:\s*(\d+)\s*\/\s*(\d+)/);
  const proposalCount = progress?.latest_result.match(/kayıtlı gerçek model önerisi:\s*(\d+|UNKNOWN)\s*\/\s*(\d+)/);
  const research = progress?.research ?? (progress && baselineCount ? {
    run_id: progressRunId ?? activeRun?.run_id ?? '',
    state: progress.latest_result.match(/Durum:\s*(\w+)/)?.[1] ?? activeRun?.state ?? 'unknown',
    phase: progress.current_work.replace(progressRunId ?? '', '').replace(/:\s*$/, '').trim(),
    baseline_completed: Number(baselineCount[1]), baseline_target: Number(baselineCount[2]),
    model_proposals: proposalCount && proposalCount[1] !== 'UNKNOWN' ? Number(proposalCount[1]) : null,
    model_proposal_limit: proposalCount ? Number(proposalCount[2]) : 0,
  } : undefined);
  const currentRun = research?.run_id && uuidPattern.test(research.run_id) ? research.run_id : activeRun?.run_id;
  const old = progress && Date.now() - Date.parse(progress.updated_at) > 2 * 60 * 1000;
  const baselineDone = !!research && research.baseline_target > 0 && research.baseline_completed >= research.baseline_target;
  const terminal = !!research && !activeRunStates.has(research.state);
  const candidateScores = progress?.latest_result.match(/Aday Scorer ölçümü:\s*(\d+)/)?.[1];
  const readableCopy = (value: string) => value.split(/([0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})/gi).map(part => uuidPattern.test(part) ? part : part.replace(/:(?=\S)/g, ': ').replace(/(\d)(?=[A-Za-zÇĞİÖŞÜçğıöşü])/g, '$1 ')).join('');
  return <section className="panel development-progress" data-testid="development-progress">
    <div className="panel-heading"><div><span className="work-eyebrow">{t("CURRENT WORK", "GÜNCEL ÇALIŞMA")}</span><h2>{t("Where are we now?", "Şu anda neredeyiz?")}</h2></div><span className={`work-live ${old || !progress ? 'waiting' : ''}`}><i/>{old ? t("Update overdue", "Güncelleme gecikti") : progress ? t("Live tracking", "Canlı takip") : t("Waiting for data", "Veri bekleniyor")}</span></div>
    {progress ? <>
      <p className="muted">{t("Source records are shown in their original language.", "Kaynak kayıtları özgün dillerinde gösterilir.")}</p>
      <h3 className="current-phase">{research?.state === 'failed' ? t("The latest research run failed — end-to-end delivery remains incomplete", "Son araştırma koşusu başarısız — uçtan uca teslim henüz tamamlanmadı") : research?.phase ?? readableCopy(progress.current_work)}</h3>
      {research && <p className="work-description">{readableCopy(progress.current_work)}</p>}
      {research && <div className="work-metrics">
        <div className={`work-metric ${baselineDone ? 'done' : ''}`}><span>{t("CPU baseline measurements", "CPU başlangıç ölçümleri")}</span><strong>{fmtNumber(research.baseline_completed)} <small>/ {fmtNumber(research.baseline_target)}</small></strong><span>{baselineDone ? t("Completed", "Tamamlandı") : t("Measurements in progress", "Ölçümler sürüyor")}</span><progress max={Math.max(1, research.baseline_target)} value={research.baseline_completed} aria-label={t("CPU baseline measurement progress", "CPU başlangıç ölçümlerinin ilerlemesi")}/></div>
        <div className="work-metric model"><span>{t("Actual local model proposals", "Gerçek yerel model önerileri")}</span><strong>{fmtMaybeNumber(research.model_proposals)} {research.model_proposal_limit > 0 && <small>{t("/ at most", "/ en fazla")} {fmtNumber(research.model_proposal_limit)}</small>}</strong><span>{research.model_proposals === null ? t("Not verified yet", "Henüz doğrulanmadı") : research.model_proposals > 0 ? t("Verified model proposal", "Doğrulanmış model önerisi") : t("No model proposals recorded yet", "Henüz kaydedilmiş model önerisi yok")}</span><p>{t("Baseline measurements and candidate experiments are excluded from this count.", "Başlangıç ölçümleri ve aday deney sayısı bu sayıya dahil değildir.")}</p></div>
        <div className="work-metric"><span>{t("Independent candidate scoring", "Bağımsız puanlanan aday ölçümü")}</span><strong>{candidateScores ?? '—'}</strong><span>{candidateScores === '0' ? t("No independent candidate score yet", "Henüz bağımsız aday puanı yok") : t("Candidate scoring record", "Aday puanlama kaydı")}</span><p>{t("A model proposal does not prove that the experiment ran successfully.", "Modelin öneri üretmesi, deneyin başarıyla çalıştığını göstermez.")}</p></div>
      </div>}
      <div className="delivery-map" aria-label={t("Project delivery status", "Projenin teslim durumu")}>
        <div><strong>{t("Completed work", "Yapılanlar")}</strong><p>{t("Experiment records, budget and ownership checks, CPU baselines and the live control interface are in place. Actual local model proposals have been obtained.", "Deney kayıtları, bütçe ve sahiplik denetimleri, CPU başlangıç ölçümleri ve canlı kontrol arayüzü kuruldu. Yerel modelden gerçek öneriler alındı.")}</p></div>
        <div><strong>{t("Current blocker", "Şu anki engel")}</strong><p>{research?.state === 'failed' && candidateScores === '0' ? t("Candidates in the last run did not reach independent scoring. The failure and safe shutdown need review.", "Son koşudaki adaylar bağımsız puanlamaya ulaşmadı. Başarısızlık nedeni ve güvenli kapanış incelenmeli.") : t("The first actual research run needs verification of its candidate experiment → independent score → report sequence and safe shutdown.", "İlk gerçek araştırmanın aday deney → bağımsız puan → sonuç raporu zinciri ve güvenli kapanışı doğrulanmalı.")}</p></div>
        <div><strong>{t("Remaining deliveries", "Kalan teslimler")}</strong><p>{t("Actual AOS GPU handover and cancellation/recovery; public-data and holdout acceptance; long experiments and training-record acceptance. See Acceptance for detailed evidence.", "Gerçek AOS ile GPU devri ve iptal/toparlanma; kamu verisi ve holdout kabulü; uzun deneyler ve eğitim kayıtlarının kabulü. Ayrıntılı kanıtlar “Kabul durumu” ekranında.")}</p></div>
      </div>
      <div className="work-result"><span className="work-label">{t("LATEST RESULT", "SON ELDE EDİLEN SONUÇ")}</span><p>{readableCopy(progress.latest_result)}</p></div>
      {research && <ol className="work-stages" aria-label={t("Research stages", "Araştırma aşamaları")}><li className={baselineDone ? 'done' : 'current'}><span>{baselineDone ? '✓' : '1'}</span><div><strong>{t("CPU baseline measurements", "CPU başlangıç ölçümleri")}</strong><small>{baselineDone ? t("Completed", "Tamamlandı") : t("In progress", "Devam ediyor")}</small></div></li><li className={baselineDone && !terminal ? 'current' : ''}><span>2</span><div><strong>{t("Local model and candidate experiments", "Yerel model ve aday deneyleri")}</strong><small>{terminal ? readableState(research.state) : baselineDone ? t("Current stage", "Güncel aşama") : t("Next", "Sırada")}</small></div></li><li><span>3</span><div><strong>{t("Result and acceptance verification", "Sonuç ve kabul doğrulaması")}</strong><small>{t("Awaiting evidence", "Kanıt bekleniyor")}</small></div></li></ol>}
      <div className="work-next"><span className="work-label">{t("NEXT STEP", "SIRADAKİ ADIM")}</span><p>{readableCopy(progress.next_step)}</p></div>
      {currentRun && <button className="current-run-link" onClick={() => onRunClick(currentRun)}><span>{t("Open current run", "Güncel koşuyu aç")} <Icon name="arrow"/></span><code>{currentRun}</code></button>}
      <div className="progress-statuses"><span>{t("CPU stop trial:", "CPU durdurma denemesi:")} <strong>{labels[progress.cpu_status]}</strong></span><span>{t("Actual GPU coexistence:", "Gerçek GPU birlikte çalışma:")} <strong>{labels[progress.gpu_status]}</strong></span></div>
      <div className="progress-updated">{t("Last progress update:", "Son çalışma güncellemesi:")} <time dateTime={progress.updated_at}>{new Date(progress.updated_at).toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'medium' })}</time>{old && t(" · Waiting for an update", " · Yeni güncelleme bekleniyor")}</div>
      <small>{t("The screen refreshes every 5 seconds. Research results and M0 acceptance require separate evidence.", "Ekran 5 saniyede bir yenilenir. Araştırma sonucu ve M0 kabulü ayrıca kanıtla doğrulanır.")}</small>
    </> : <p>{t("The current development record is not available yet.", "Güncel geliştirme kaydı henüz alınamadı.")}</p>}
  </section>;
}

function SystemSummary({ overview }: { overview: Overview | null }) {
  const m = overview?.system.memory; const gpu = overview?.system.gpu;
  const rows = [
    ['RAM', m ? t(`${fmtBytes(m.available_bytes)} available / ${fmtBytes(m.total_bytes)} total`, `${fmtBytes(m.available_bytes)} kullanılabilir / ${fmtBytes(m.total_bytes)} toplam`) : t("Waiting for live measurements", "Canlı ölçüm bekleniyor"), m ? 'ready' : 'unknown'],
    ['GPU', gpu?.available ? `${gpu.name ?? 'GPU'} · ${fmtMaybeNumber(gpu.used_mib)} / ${fmtMaybeNumber(gpu.total_mib)} MiB` : gpu?.reason ?? t("Waiting for live measurements", "Canlı ölçüm bekleniyor"), gpu?.available ? 'ready' : 'unknown'],
    ['Lab API', overview?.lab.connected ? t("Connected", "Bağlı") : reasonText(overview?.lab.reason) ?? t("Disconnected", "Bağlı değil"), overview?.lab.connected ? 'ready' : 'offline'],
  ] as const;
  return <section className="panel system-summary"><div className="panel-heading"><h2>{t("Local system", "Yerel sistem")}</h2></div>{rows.map(([label, value, state]) => <div className="system-row" key={label}><strong>{label}</strong><span className={`dot ${state}`}/><span>{value}</span></div>)}{overview && !overview.lab.model_runs_enabled && <div className="warning-note"><span className="warning-mark">!</span>{t("Starting new model runs from the console is disabled. Existing runs can be monitored.", "Konsoldan yeni model koşusu başlatma kapalı. Mevcut koşuları izleyebilirsiniz.")}</div>}</section>;
}
function RunRow({ run, onSelect }: { run: Run; onSelect: (id: string) => void }) { return <button className="run-row" onClick={() => onSelect(run.run_id)}><span className={`run-state ${activeRunStates.has(run.state) ? 'active' : ''}`}>{purposeText(run.purpose)} · {readableState(run.state)}{run.stale || run.unavailable ? t(" · Stale status", " · Eski durum") : ''}</span><code>{run.run_id}</code><span>{fmtTime(run.updated_at ?? run.created_at)}</span><span className="row-chevron">›</span></button>; }
function SystemPage({ overview, apiError, checks, onStartCheck, busy }: { overview: Overview | null; apiError: string | null; checks: Check[]; onStartCheck: () => void; busy: boolean }) {
  return <div className="page-stack"><section className="panel"><div className="panel-heading"><div><h2>{t("System status", "Sistem durumu")}</h2><p>{t("Latest live API measurements only", "Yalnız API’nin son canlı ölçümleri")}</p></div><span className={`live-label ${apiError ? 'offline' : overview ? 'online' : ''}`}><i/>{apiError ? t("Disconnected", "Bağlı değil") : overview ? t("Live", "Canlı") : t("Waiting", "Bekleniyor")}</span></div>{!overview ? <EmptyState title={t("No system measurements", "Sistem ölçümü yok")} text={apiError || t("Waiting for the local API response.", "Yerel API yanıtı bekleniyor.")}/> : <div className="metrics-grid"><Metric label={t("Memory", "Bellek")} value={fmtBytes(overview.system.memory.total_bytes)} detail={t(`${fmtBytes(overview.system.memory.available_bytes)} available`, `${fmtBytes(overview.system.memory.available_bytes)} kullanılabilir`)}/><Metric label={t("Disk", "Disk")} value={fmtBytes(overview.system.disk.total_bytes)} detail={t(`${fmtBytes(overview.system.disk.free_bytes)} free`, `${fmtBytes(overview.system.disk.free_bytes)} boş`)}/><Metric label="CPU" value={t(`${overview.system.cpu.logical_count.toLocaleString(locale())} logical cores`, `${overview.system.cpu.logical_count.toLocaleString(locale())} mantıksal çekirdek`)} detail={overview.system.cpu.load_1m == null ? t("No load measurement", "Yük ölçümü yok") : t(`1 min load average ${fmtNumber(overview.system.cpu.load_1m, 2)}`, `1 dk yük ortalaması ${fmtNumber(overview.system.cpu.load_1m, 2)}`)}/><Metric label="GPU" value={overview.system.gpu.available ? overview.system.gpu.name ?? t("Available", "Kullanılabilir") : t("Unavailable", "Kullanılamıyor")} detail={overview.system.gpu.available ? `${fmtMaybeNumber(overview.system.gpu.used_mib)} / ${fmtMaybeNumber(overview.system.gpu.total_mib)} MiB · ${fmtMaybeNumber(overview.system.gpu.utilization_percent)}%` : overview.system.gpu.reason || t("No measurement", "Ölçüm yok")}/></div>}</section>
    <section className="panel"><div className="panel-heading"><div><h2>{t("CPU check", "CPU kontrolü")}</h2><p>{t("Authorized quick quality check; it does not change the acceptance count.", "İzinli hızlı kalite komutu; kabul sayısını değiştirmez.")}</p></div><button className="button primary" onClick={onStartCheck} disabled={busy || checks.some(item => item.state === 'queued' || item.state === 'running')}><Icon name="check"/>{t("Run check", "Kontrolü çalıştır")}</button></div>{checks.length ? <div className="check-history">{checks.map((check, index) => <details className="check-entry" key={check.id} data-testid={index === 0 ? 'check-result' : undefined} open={index === 0}><summary><span className={`status-pill ${check.state === 'passed' ? 'passed' : check.state === 'failed' ? 'open' : 'partial'}`}>{readableState(check.state)}</span><code>{check.id}</code><span>{fmtTime(check.started_at)}</span><span>{t("Exit code:", "Çıkış kodu:")} {check.exit_code ?? '—'}</span></summary><div className="check-result"><p className="muted">{t("Source records are shown in their original language.", "Kaynak kayıtları özgün dillerinde gösterilir.")}</p><strong>{check.summary || t("No check summary", "Kontrol özeti yok")}</strong><pre>{check.output || t("No check output.", "Kontrol çıktısı yok.")}</pre></div></details>)}</div> : <EmptyState title={t("No checks have run yet", "Henüz kontrol çalıştırılmadı")} text={t("After a check runs, its actual process result and bounded output appear here.", "Çalıştırıldığında gerçek süreç sonucu ve sınırlı çıktısı burada görünür.")}/>}</section>
    </div>;
}
function Metric({ label, value, detail }: { label: string; value: string | null; detail: string }) { return <div className="metric"><span>{label}</span><strong>{value ?? t("No measurement", "Ölçüm yok")}</strong><small>{detail}</small></div>; }
function ExperimentsPage({ overview, runs, apiError, busy, onNew, onWatch, watchId, setWatchId, onSelect, onReport, onStop }: {
  overview: Overview | null; runs: Run[]; apiError: string | null; busy: boolean; onNew: () => void;
  onWatch: (event: React.FormEvent) => void; watchId: string; setWatchId: (value: string) => void;
  onSelect: (id: string) => void; onReport: (id: string) => void; onStop: (id: string) => void;
}) {
  return <div className="page-stack"><section className="panel"><div className="panel-heading"><div><h2>{t("Experiments", "Deneyler")}</h2><p>{t("Runs verified and monitored through the local Lab API", "Yerel Lab API üzerinden doğrulanıp izlenen koşular")}</p></div><button className="button primary" onClick={onNew} disabled={!canRunAnySuite(overview?.lab)} title={!overview?.lab.connected ? reasonText(overview?.lab.reason) ?? t("Lab API disconnected", "Lab API bağlı değil") : undefined}>{t("Start a new experiment", "Yeni deney başlat")}</button></div>
    {!overview?.lab.connected && <div className="connection-inline"><strong>{t("Cannot start an experiment", "Deney başlatılamıyor")}</strong><p>{apiError || reasonText(overview?.lab.reason) || t("The Lab API is disconnected. Starting and tracking runs require an actual API response.", "Lab API bağlantısı kurulmadı. Koşu başlatma ve izleme gerçek API yanıtı gerektirir.")}</p></div>}
    {overview?.lab.connected && !canRunAnySuite(overview.lab) && <div className="connection-inline"><strong>{t("Model experiments disabled", "Model deneyleri kapalı")}</strong><p>{reasonText(overview.lab.reason) || t("No registered suite is currently runnable.", "Kayıtlı bir suite şu an çalıştırılabilir değil.")}</p></div>}
    {runs.length ? <div className="runs-table"><div className="table-head"><span>{t("Status", "Durum")}</span><span>{t("Run UUID", "Koşu UUID")}</span><span>{t("Last updated", "Son güncelleme")}</span><span>{t("Actions", "Eylemler")}</span></div>{runs.map(run => <div className="runs-table-row" key={run.run_id}><span className={`run-state ${activeRunStates.has(run.state) ? 'active' : ''}`}>{purposeText(run.purpose)} · {readableState(run.state)}{run.stale || run.unavailable ? t(" · Unavailable", " · Ulaşılamıyor") : ''}</span><button className="uuid-button" onClick={() => onSelect(run.run_id)}>{run.run_id}</button><span>{fmtTime(run.updated_at ?? run.created_at)}</span><span className="row-actions"><button className="button secondary tiny" onClick={() => onSelect(run.run_id)}>{t("Status", "Durum")}</button><button className="button secondary tiny" onClick={() => onReport(run.run_id)} disabled={!run.report_sha256}>{t("Report", "Rapor")}</button><button className="button secondary tiny danger-text" onClick={() => onStop(run.run_id)} disabled={busy || !stoppableRunStates.has(run.state)}>{run.state === 'stop_requested' ? t("Stop requested…", "Durdurma bekleniyor…") : t("Stop", "Durdur")}</button></span></div>)}</div> : <EmptyState title={apiError ? t("Could not load runs", "Koşular yüklenemedi") : t("No tracked runs yet", "Henüz izlenen koşu yok")} text={apiError || t("Start an experiment or verify and track an existing Lab run by UUID.", "Yeni deney başlatın veya mevcut Lab koşusunu UUID ile doğrulayıp izlemeye ekleyin.")}/>}
  </section><section className="panel watch-panel"><h2>{t("Track an existing run", "Mevcut koşuyu izle")}</h2><p>{t("A run is added only after actual API verification.", "Koşu yalnız gerçek API doğrulamasından sonra listeye eklenir.")}</p><form className="watch-form left" onSubmit={onWatch}><input aria-label={t("Run UUID", "Koşu UUID’si")} value={watchId} onChange={event => setWatchId(event.target.value)} placeholder={t("Run ID (UUID)", "Koşu kimliği (UUID)")} autoComplete="off"/><button className="button primary" type="submit" disabled={busy || !watchId.trim()}>{t("Track", "İzle")}</button></form></section></div>;
}
function AcceptanceDialog({ item, onClose, onEvidence, evidence }: { item: AcceptanceItem; onClose: () => void; onEvidence: (ref: AcceptanceItem['evidence'][number]) => void; evidence: { ref: AcceptanceItem['evidence'][number]; data?: EvidenceResponse; error?: string } | null }) {
  useEscape(onClose);
  return <div className="overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><section className="dialog" role="dialog" aria-modal="true" aria-labelledby="acceptance-dialog-title" tabIndex={-1}><div className="dialog-header"><div><span className="eyebrow">{item.id}</span><h2 id="acceptance-dialog-title">{item.title}</h2></div><button className="icon-button" onClick={onClose} aria-label={t("Close", "Kapat")}><Icon name="close"/></button></div><div className="dialog-body"><StatusPill status={item.status}/><p className="muted">{t("Source records are shown in their original language.", "Kaynak kayıtları özgün dillerinde gösterilir.")}</p><p className="detail-copy">{item.detail}</p><h3>{t("Evidence", "Kanıtlar")}</h3>{item.evidence.length ? item.evidence.map(ref => <button className="evidence-link" key={ref.url} onClick={() => onEvidence(ref)}><Icon name="file"/><span>{ref.name}</span><small>{ref.kind}</small></button>) : <p className="muted">{t("No evidence links are registered for this item.", "Bu madde için kayıtlı kanıt bağlantısı yok.")}</p>}{evidence && <div className="evidence-content"><div className="evidence-title"><strong>{evidence.ref.name}</strong><button className="icon-button" onClick={() => onEvidence(evidence.ref)} aria-label={t("Reload evidence", "Kanıtı yeniden yükle")}><Icon name="refresh"/></button></div>{evidence.error ? <p className="error-text">{evidence.error}</p> : evidence.data ? <>{evidence.data.truncated && <p className="muted">{t("Content was truncated at the 256 KiB limit.", "İçerik 256 KiB sınırında kısaltıldı.")}</p>}{evidence.data.kind === 'html' ? <iframe className="evidence-frame" title={evidence.data.name} sandbox="" srcDoc={evidence.data.content}/> : <><pre>{evidence.data.content}</pre><small className="evidence-hash">SHA-256 {evidence.data.sha256}</small></>}</> : <p className="muted">{t("Loading evidence…", "Kanıt yükleniyor…")}</p>}</div>}</div></section></div>;
}
function RunDialog({ overview, form, setForm, error, busy, onSubmit, onClose }: {
  overview: Overview | null; form: RunForm;
  setForm: (value: RunForm) => void;
  error: string | null; busy: boolean; onSubmit: (event: React.FormEvent) => void; onClose: () => void;
}) {
  useEscape(onClose);
  const suite = overview?.lab.suites.find(item => item.suite_id === form.suite);
  const disabled = busy || !canRunSuite(overview?.lab, suite, form.purpose);
  return <div className="overlay" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}><section className="dialog form-dialog" role="dialog" aria-modal="true" aria-labelledby="run-dialog-title" tabIndex={-1}><div className="dialog-header"><div><span className="eyebrow">LAB API</span><h2 id="run-dialog-title">{t("New experiment", "Yeni deney")}</h2></div><button className="icon-button" onClick={onClose} aria-label={t("Close", "Kapat")}><Icon name="close"/></button></div><form className="dialog-body run-form" onSubmit={onSubmit}><p className="muted">{t("Only registered suites and defined budget limits are available.", "Yalnız kayıtlı suite’ler ve tanımlı bütçe sınırları kullanılabilir.")}</p><label>{t("Operation", "İşlem")}<select value={form.purpose} onChange={event => setForm({ ...form, purpose: event.target.value as RunForm['purpose'] })}><option value="baseline">{t("CPU baseline · no model", "CPU baseline · model kullanmaz")}</option><option value="research">{t("Research · registered provider", "Araştırma · kayıtlı sağlayıcı")}</option></select></label><p className="muted">{form.purpose === 'baseline' ? t("CPU baselines on registered data; proposal and model-token budgets are zero.", "Kayıtlı veri üzerinde CPU baseline ölçümü; öneri ve model token bütçesi sıfırdır.") : suite?.provider === 'fake-json' ? t("Fixture research uses prepared proposals; it is not actual model evidence.", "Fixture araştırması: hazır öneriler kullanır, gerçek model kanıtı değildir.") : t("Local model research requires authorization to run models.", "Yerel model ile araştırma; model çalıştırma izni gerekir.")}</p><label>Suite<select required value={form.suite} onChange={event => { const nextSuite = overview?.lab.suites.find(item => item.suite_id === event.target.value); const cap = Math.min(35, nextSuite?.proposal_limit ?? 35); setForm({ ...form, suite: event.target.value, experiments: Math.min(form.experiments, cap) }); }} disabled={!overview?.lab.suites.length}><option value="">{t("Select a suite", "Suite seçin")}</option>{overview?.lab.suites.map(item => <option key={item.suite_id} value={item.suite_id}>{item.suite_id} · {item.track} · {item.provider}</option>)}</select></label>{suite && <div className="suite-meta"><span>{t("Track:", "İz:")} {suite.track}</span><span>{t("Program:", "Program:")} {suite.program_version}</span><span>{t("Proposal limit:", "Proposal tavanı:")} {fmtNumber(suite.proposal_limit)}</span></div>}{form.purpose === 'research' && <label>{t("Experiment count", "Deney adedi")}<input type="number" min="1" max={Math.min(35, suite?.proposal_limit ?? 35)} value={form.experiments} onChange={event => setForm({ ...form, experiments: Number(event.target.value) })} required/></label>}<label>{t("Time limit (seconds)", "Süre sınırı (saniye)")}<input type="number" min="1" max={Math.min(14400, suite?.max_wall_seconds ?? 14400)} value={form.wall_seconds} onChange={event => setForm({ ...form, wall_seconds: Number(event.target.value) })} required/></label>{form.purpose === 'research' && <label>{t("Model token limit", "Model token sınırı")}<input type="number" min="0" max={Math.min(350000, suite?.max_model_tokens ?? 350000)} value={form.model_tokens} onChange={event => setForm({ ...form, model_tokens: Number(event.target.value) })} required/></label>}{!overview?.lab.connected && <div className="connection-inline"><strong>{t("Starting unavailable", "Başlatma kullanılamıyor")}</strong><p>{reasonText(overview?.lab.reason) || t("Lab API disconnected.", "Lab API bağlı değil.")}</p></div>}{overview?.lab.connected && suite && !canRunSuite(overview.lab, suite, form.purpose) && <div className="connection-inline"><strong>{t("This suite is unavailable", "Bu suite kullanılamıyor")}</strong><p>{reasonText(overview.lab.reason) || t("Model runs are disabled for the selected suite.", "Seçili suite için model çalıştırma etkin değil.")}</p></div>}{error && <div className="field-error" role="alert">{error}</div>}<div className="dialog-actions"><button className="button secondary" type="button" onClick={onClose} disabled={busy}>{t("Cancel", "Vazgeç")}</button><button className="button primary" type="submit" disabled={disabled}>{busy ? t("Starting…", "Başlatılıyor…") : t(`Start ${purposeText(form.purpose)}`, `${purposeText(form.purpose)} başlat`)}</button></div></form></section></div>;
}
function RunDrawer({ id, detail, report, onClose, onStop, busy }: { id: string; detail: Run | null; report: unknown; onClose: () => void; onStop: (id: string) => void; busy: boolean }) {
  useEscape(onClose);
  return <div className="overlay drawer-overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><aside className="run-drawer" role="dialog" aria-modal="true" aria-labelledby="run-drawer-title" tabIndex={-1}><div className="dialog-header"><div><span className="eyebrow">{t("RUN STATUS", "KOŞU DURUMU")}</span><h2 id="run-drawer-title">{t("Experiment details", "Deney ayrıntısı")}</h2></div><button className="icon-button" aria-label={t("Close", "Kapat")} onClick={onClose}><Icon name="close"/></button></div><div className="dialog-body"><span className={`run-state ${detail && activeRunStates.has(detail.state) ? 'active' : ''}`}>{detail ? readableState(detail.state) : t("Loading status…", "Durum yükleniyor…")}</span><label className="field-label">{t("Run UUID", "Koşu UUID")}</label><code className="uuid-box">{id}</code>{detail && <dl className="run-meta"><dt>{t("Operation", "İşlem")}</dt><dd>{purposeText(detail.purpose)}</dd><dt>{t("Created", "Oluşturulma")}</dt><dd>{fmtTime(detail.created_at)}</dd><dt>{t("Updated", "Güncelleme")}</dt><dd>{fmtTime(detail.updated_at)}</dd><dt>{t("Source", "Kaynak")}</dt><dd>{detail.origin ?? '—'}</dd><dt>{t("Stop request", "Durdurma isteği")}</dt><dd>{detail.stop_requested ? t("Sent", "İletildi") : t("None", "Yok")}</dd><dt>{t("Report SHA-256", "Rapor SHA-256")}</dt><dd>{detail.report_sha256 ?? t("Not available yet", "Henüz yok")}</dd></dl>}{detail && activeRunStates.has(detail.state) && <button className="button danger" onClick={() => onStop(id)} disabled={busy || !stoppableRunStates.has(detail.state)}>{detail.state === 'stop_requested' ? t("Stop requested…", "Durdurma bekleniyor…") : t("Stop experiment", "Deneyi durdur")}</button>}{detail?.purpose === 'mode-stream' && <ModeStreamProgress key={id} runId={id}/>} {report !== null && <div className="report-view"><p className="muted">{t("Source records are shown in their original language.", "Kaynak kayıtları özgün dillerinde gösterilir.")}</p><h3>{t("Verified report", "Doğrulanmış rapor")}</h3><button className="button secondary" onClick={() => { const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' })); const link = document.createElement('a'); link.href = url; link.download = `lab-report-${id}.json`; link.click(); URL.revokeObjectURL(url); }}>{t("Download report JSON", "Rapor JSON indir")}</button><ModeDiagnostics runId={id} report={report}/><pre>{JSON.stringify(report, null, 2)}</pre></div>}</div></aside></div>;
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
