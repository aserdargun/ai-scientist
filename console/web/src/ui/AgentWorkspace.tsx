import { useEffect, useState } from 'react';
import type { AcceptanceItem, FieldIntent, Overview, PriorExperience, Run, RunExperience } from './types';
import { ExperiencePanel } from './ExperiencePanel';

type Props = {
  overview: Overview | null; runs: Run[];
  onModes: () => void; onExperiments: () => void; onAnomaly: () => void;
  onRun: (id: string) => void; onReport: (id: string) => void;
  onEvidence: (item: AcceptanceItem) => void;
  onDesignWithHistory: (prior: PriorExperience) => void;
  fieldIntentDraft: FieldIntent; onFieldIntentDraftChange: (intent: FieldIntent) => void;
  onDesignWithFieldIntent: (intent: FieldIntent) => void; onClearFieldIntent: () => void;
};
const activeStates = new Set(['queued', 'running', 'stop_requested']);
const runStates: Record<string, string> = { queued: 'Kuyrukta', running: 'Çalışıyor', stop_requested: 'Durdurma bekleniyor', completed: 'Tamamlandı', stopped: 'Durduruldu', failed: 'Başarısız', cancelled: 'İptal edildi' };
const purposes: Record<string, string> = { research: 'Araştırma', baseline: 'CPU başlangıç ölçümü', 'mode-grid': 'Yöntem karşılaştırması', 'mode-stream': 'CPU OMR tanısı · puanlanmaz' };
const time = (value?: string) => value ? new Date(value).toLocaleString('tr-TR', { dateStyle: 'medium', timeStyle: 'short' }) : 'Zaman kaydı yok';
const freshRun = (run: Run) => !run.stale && !run.unavailable;
function observedCapacity(system?: Overview['system']) {
  const ram = system?.memory.total_bytes;
  const vram = system?.gpu.total_mib;
  return {
    ram_gib: ram != null && Number.isFinite(ram) && ram > 0 ? ram / 1024 ** 3 : null,
    vram_gib: vram != null && Number.isFinite(vram) && vram > 0 ? vram / 1024 : null,
    logical_cpu_count: system?.cpu.logical_count ?? null,
  };
}

export function AgentWorkspace({ overview, runs, onModes, onExperiments, onRun, onReport, onEvidence, onAnomaly, onDesignWithHistory, fieldIntentDraft, onFieldIntentDraftChange, onDesignWithFieldIntent, onClearFieldIntent }: Props) {
  const [experience, setExperience] = useState<RunExperience | null>(null);
  const connected = !!overview?.lab.connected;
  const currentExperience = connected && experience && runs.some(run => freshRun(run) && ['completed', 'stopped', 'failed'].includes(run.state) && run.run_id === experience.run_id && run.report_sha256 === experience.report_sha256) ? experience : null;
  const capacity = observedCapacity(overview?.system);
  const modelEnabled = connected && !!overview?.lab.model_runs_enabled;
  const active = runs.filter(run => freshRun(run) && activeStates.has(run.state));
  const latest = [...runs].sort((a, b) => Date.parse(b.updated_at ?? b.created_at ?? '') - Date.parse(a.updated_at ?? a.created_at ?? ''));
  const reports = runs.filter(run => freshRun(run) && run.report_sha256);
  const anomaly = connected && !!overview?.lab.suites.some(suite => suite.track === 'anomaly');
  const progress = overview?.development_progress;
  const evidence = (id: string) => overview?.acceptance.items.find(item => item.id === id);
  const modelEvidence = evidence('M0.13');
  const connectionReason = overview?.lab.reason?.includes('Lab API is not configured') ? 'Lab API henüz yapılandırılmadı.' : overview?.lab.reason;
  const next = !connected ? 'Lab bağlantısını doğrula; mevcut kayıt ve kanıtları incele.' : active.length ? 'Aktif işi izle; terminal rapor doğrulanana kadar sonucu bekle.' : 'Veriyi seç, istatistiğini incele ve süre bütçesiyle bir CPU karşılaştırması başlat.';
  const capabilities = [
    { name: 'Veri seçimi ve kalite', description: 'Yetkili PostgreSQL kaynağı, varlık ve UTC aralığı veya sentetik snapshot. DB kaynağı ayrıca yapılandırılır.', state: connected ? 'Veri yolu açık' : 'Bağlantı gerekli', ready: connected, label: 'Veri seç', action: onModes },
    { name: 'İstatistik ve sensör ilişkileri', description: 'Dağılım, eksikler, korelasyon ve otokorelasyon. Etiketsiz veri için tanımlayıcı analiz.', state: connected ? 'CPU analizi açık' : 'Bağlantı gerekli', ready: connected, label: 'Analizi aç', action: onModes },
    { name: 'LSH / OPTICS / SOM', description: 'Çalışma modu öğrenme, yöntem hiperparametreleri ve kontrollü karşılaştırma. Algoritmalar mevcut.', state: connected ? 'CPU deney yolu açık' : 'Bağlantı gerekli', ready: connected, label: 'Yöntemleri karşılaştır', action: onModes },
    { name: 'NN, residual ve OMR', description: 'Mod toleransı → normal değer tahmini → sensör residual ve OMR. Mod/SOM uzaklıkları ayrı kalır; uzun tanı akışı puanlanmaz.', state: connected ? 'CPU akış yolu açık' : 'Bağlantı gerekli', ready: connected, label: 'OMR akışını aç', action: onModes },
    { name: 'Anomali karşılaştırması', description: 'Kayıtlı süit üzerinde bağımsız Scorer ve Referee. Yanlış alarm ve erken yakalama için uygun etiket gerekir.', state: anomaly ? 'Kayıtlı süit var' : connected ? 'Süit gerekli' : 'Bağlantı gerekli', ready: anomaly, label: 'Deney seç', action: onAnomaly },
    { name: 'Yerel araştırma modeli', description: 'Qwen S1/S2 ile sonlu yöntem/hiperparametre önerileri. Model erişimi, kaynak kabulü ve bağımsız ölçüm gerekir.', state: modelEnabled ? 'Model yolu açık' : 'Model çağrıları kapalı', ready: modelEnabled, label: 'Araştırmayı yapılandır', action: onModes },
  ];
  return <div className="agent-workspace page-stack" data-testid="agent-workspace">
    <nav className="agent-section-links" aria-label="Eylemci bölümleri"><a href="#agent-status">Durum</a><a href="#agent-field-intent">Saha amacı</a><a href="#agent-capabilities">Yetenekler</a><a href="#agent-experience">Deney hafızası</a><a href="#teacher-draft">Öğretmen</a></nav>
    <section className="panel agent-status" id="agent-status" aria-labelledby="agent-status-title">
      <div className="panel-heading"><div><h2 id="agent-status-title">Eylemci durumu</h2><p>Dijital ikiz ve kestirimci bakım için yerel araştırma çalışma alanı</p></div><span className={`agent-label ${connected ? 'available' : 'waiting'}`}>{connected ? 'Lab bağlı' : 'Lab bağlı değil'}</span></div>
      <div className="agent-status-body"><div>
        <h3>{!overview ? 'Canlı durum bekleniyor' : !connected ? 'Deney başlatmak için bağlantı gerekiyor' : active.length ? `${active.length} aktif iş izleniyor` : 'Aktif iş yok'}</h3>
        <p>{!overview ? 'API yanıtı gelmeden çalışma durumu veya kaynak uygunluğu doğrulanamaz.' : !connected ? connectionReason || 'Bu kurulumda Lab API bağlantısı kurulmadı.' : modelEnabled ? 'CPU deneyleri ve yerel model yolu açık. Yeni koşu sunucunun bütçe ve kaynak kabulünden geçer.' : 'CPU yolu açık; yerel model çağrıları kapalı. Sıfır token ile yöntem karşılaştırması yapılabilir.'}</p>
        {active.slice(0, 2).map(run => <button className="agent-active-run" key={run.run_id} onClick={() => onRun(run.run_id)}><span>{runStates[run.state] ?? run.state} · {purposes[run.purpose ?? ''] ?? 'Koşu'}</span><code>{run.run_id}</code><span>İzle / durdur</span></button>)}
        {runs.some(run => !freshRun(run)) && <p className="agent-note">Bazı izlenen kayıtlar güncel okunamadı; aktif iş sayısına dahil edilmedi.</p>}
      </div><div className="agent-next"><strong>Sıradaki eylem</strong><p>{next}</p><div className="agent-actions"><button className="button primary" onClick={active.length || !connected ? onExperiments : onModes}>{active.length || !connected ? 'Koşuları incele' : 'Veri ve yöntem seç'}</button><button className="button secondary" onClick={onExperiments}>Geçmiş ve raporlar</button></div></div></div>
      <div className="agent-connection"><span>Yerel model: <strong>{modelEnabled ? 'Çağrı yolu açık' : 'Kapalı'}</strong></span><span>AOS birlikte çalışma: <strong>{evidence('M0.AOS.7')?.status === 'passed' ? 'Kayıtlı kabul geçti' : 'Henüz doğrulanmadı'}</strong></span><span>Veri zamanı: <strong>{overview ? time(overview.generated_at) : 'Bekleniyor'}</strong></span></div>
      <p className="agent-note">Gözlenen host: RAM {capacity.ram_gib === null ? 'bilinmiyor' : `${capacity.ram_gib.toFixed(1)} GiB`}, VRAM {capacity.vram_gib === null ? 'bilinmiyor' : `${capacity.vram_gib.toFixed(1)} GiB`}, mantıksal CPU {capacity.logical_cpu_count ?? 'bilinmiyor'}. AOS ile toplam CPU/RAM/disk kabulü ve tek GPU sırası; sınırlı model çağrıları, rezerv ve kaynak devri gerekir. Toplam kapasite, boş kaynak veya eğitim tahsisi değildir.</p>
    </section>

    <FieldIntentForm value={fieldIntentDraft} connected={connected} onChange={onFieldIntentDraftChange} onDesign={onDesignWithFieldIntent} onClear={onClearFieldIntent}/>
    <section className="panel" id="agent-capabilities" aria-labelledby="agent-capabilities-title"><div className="panel-heading"><div><h2 id="agent-capabilities-title">Genel yetenekler</h2><p>Algoritmanın mevcut olması ve bu kurulumdaki çalışma yolu ayrı gösterilir.</p></div>{modelEvidence && <button className="button secondary small" onClick={() => onEvidence(modelEvidence)}>Model kanıtı</button>}</div>
      <div className="agent-capabilities">{capabilities.map(item => <div className="agent-capability" key={item.name}><div><h3>{item.name}</h3><p>{item.description}</p></div><span className={`agent-label ${item.ready ? 'available' : 'waiting'}`}>{item.state}</span><button className="button secondary small" onClick={item.action} disabled={!item.ready}>{item.label}</button></div>)}
        <div className="agent-capability"><div><h3>Öğretmen model ve eğitim</h3><p>Yerel öğretmen, veri referansı, amaç ve bütçe taslağı hazırlanır. Eğitim çalıştırma API'si ve öğrenilmiş adapter henüz yok.</p></div><span className="agent-label waiting">Taslak hazırlama</span><a className="button secondary small" href="#teacher-draft">Taslağı hazırla</a></div>
        <div className="agent-capability"><div><h3>Skill ve sürekli gelişim</h3><p>Doğrulanmış geliştirme bulguları açık seçimle yeni deney bağlamına taşınabilir. Otomatik geçmiş taraması yok. Skill yayını ve otomatik yeni model sürümü için bağımsız değerlendirme, holdout ve rollback gerekir.</p></div><span className="agent-label waiting">Gelişim zinciri kısmi</span><a className="button secondary small" href="#agent-experience">Hafızayı incele</a></div>
      </div>
    </section>

    <section className="panel" id="agent-learning" aria-labelledby="agent-learning-title"><div className="panel-heading"><div><h2 id="agent-learning-title">Deney ve gelişim geçmişi</h2><p>Yalnız bağlı gerçek koşu kayıtları. Bir koşunun bitmesi, eylemcinin iyileştiğini göstermez.</p></div><button className="button secondary small" onClick={onExperiments}>Tüm koşular</button></div>
      <ol className="agent-learning-path"><li><strong>Deney kaydı</strong><span>{runs.length} izlenen kayıt</span></li><li><strong>Bağımsız rapor</strong><span>{reports.length} güncel rapor referansı</span></li><li><strong>Holdout / karşılaştırma</strong><span>Her kararın kanıtı gerekir</span></li><li><strong>Skill / yeni model</strong><span>Otomatik terfi hazır değil</span></li></ol>
      <p className="agent-note">KEEP, geliştirme kümesinde seçimdir. Kalıcı gelişim; bağımsız doğrulama, uygun eğitim kaydı ve yeni sürüm karşılaştırmasıyla gösterilir. DISCARD veya başarısız deney de kayıt olarak korunur.</p>
      {latest.length ? <div className="agent-history">{latest.slice(0, 5).map(run => <div className="agent-history-row" key={run.run_id}><div><button className="uuid-button" onClick={() => onRun(run.run_id)}>{run.run_id}</button><span>{purposes[run.purpose ?? ''] ?? 'Tür kaydı yok'} · {time(run.updated_at ?? run.created_at)}</span></div><span className={`agent-label ${freshRun(run) && run.state === 'completed' ? 'available' : 'waiting'}`}>{!freshRun(run) ? 'Güncel okunamadı' : runStates[run.state] ?? run.state}</span><div><span className="agent-history-outcome">{run.purpose === 'mode-stream' ? 'Tanı akışı; gelişim puanı yok' : 'Gelişim sonucu rapordan değerlendirilir'}</span><button className="button secondary small" onClick={() => onReport(run.run_id)} disabled={!freshRun(run) || !run.report_sha256}>Raporu aç</button></div></div>)}</div> : <div className="empty-state"><strong>Henüz bağlı koşu kaydı yok</strong><span>Deneyler ekranından kayıtlı bir koşuyu izlemeye ekleyin veya veri/yöntem akışından başlayın. Geçmiş örnek kayıtlarla doldurulmaz.</span></div>}
      {progress && <div className="agent-development"><strong>Geliştirme kaydı · {time(progress.updated_at)}</strong><p>{progress.latest_result}</p><details><summary>Açık teslimler</summary><p>{progress.next_step}</p></details></div>}
    </section>

    <ExperiencePanel runs={runs} connected={connected} onVerifiedChange={setExperience} onReport={onReport} onDesign={onDesignWithHistory}/>
    <TeacherDraft runs={reports.filter(run => run.state === 'completed' && run.purpose !== 'mode-stream')} experience={currentExperience} system={overview?.system} observedAt={overview?.generated_at} trainingEvidence={evidence('M0.14')} onEvidence={onEvidence}/>
  </div>;
}

function FieldIntentForm({ value, connected, onChange, onDesign, onClear }: { value: FieldIntent; connected: boolean; onChange: (intent: FieldIntent) => void; onDesign: (intent: FieldIntent) => void; onClear: () => void }) {
  const intent = { ...value, asset_id: value.asset_id.trim(), objective: value.objective.trim() };
  const assetLength = Array.from(intent.asset_id).length, objectiveLength = Array.from(intent.objective).length;
  const controls = /[\u0000-\u001f\u007f]/.test(intent.asset_id + intent.objective);
  const valid = assetLength >= 1 && assetLength <= 128 && objectiveLength >= 1 && objectiveLength <= 600 && !controls;
  return <section className="panel" id="agent-field-intent" aria-labelledby="field-intent-title"><div className="panel-heading"><div><h2 id="field-intent-title">Saha araştırma amacı</h2><p>Varlık etiketini ve hedefi yazın; veri, yöntem ve bütçeyi sonraki adımda seçin.</p></div><span className="agent-label waiting">Kullanıcı beyanı</span></div>
    <form className="run-form" onSubmit={event => { event.preventDefault(); if (connected && valid) onDesign(intent); }}>
      <label>Varlık etiketi<input value={value.asset_id} maxLength={256} onChange={event => onChange({ ...value, asset_id: event.target.value })} placeholder="Örn. pompa-01"/><span>{assetLength}/128 karakter</span></label>
      <label>Araştırma amacı<select value={value.goal_kind} onChange={event => onChange({ ...value, goal_kind: event.target.value as FieldIntent['goal_kind'] })}><option value="digital_twin">Dijital ikiz araştırması</option><option value="predictive_maintenance">Kestirimci bakım araştırması</option></select></label>
      <label>Çalışma hedefi<input value={value.objective} maxLength={1200} onChange={event => onChange({ ...value, objective: event.target.value })} placeholder="Örn. yük değişimlerinde normal çalışma modlarını karşılaştır"/><span>{objectiveLength}/600 karakter</span></label>
      {(controls || assetLength > 128 || objectiveLength > 600) && <p role="alert" className="field-error">Varlık en fazla 128, hedef en fazla 600 karakter olabilir; kontrol karakterleri kullanılamaz.</p>}
      <div className="agent-actions"><button className="button primary" type="submit" disabled={!connected || !valid}>Bu amaçla deney tasarla</button><button className="button secondary" type="button" onClick={onClear} disabled={!value.asset_id && !value.objective}>Amacı temizle</button></div>
    </form><p className="agent-note">Varlık etiketi sizin beyanınızdır; yetkili kaynak/varlık eşlemesi veya bakım aksiyonu kanıtı değildir. Hedef yöntem, skor ve bütçeyi otomatik değiştirmez. Taslağın gönderilmesi kullanıldığı anlamına gelmez; koşu sonunda bağlam kanıtı ayrıca okunur.</p>
  </section>;
}

function TeacherDraft({ runs, experience, system, observedAt, trainingEvidence, onEvidence }: { runs: Run[]; experience: RunExperience | null; system?: Overview['system']; observedAt?: string; trainingEvidence?: AcceptanceItem; onEvidence: (item: AcceptanceItem) => void }) {
  const [teacher, setTeacher] = useState('Qwen/Qwen3.5-9B');
  const [student, setStudent] = useState('Yerel araştırma eylemcisi');
  const [purpose, setPurpose] = useState('teacher_examples');
  const [objective, setObjective] = useState('Çalışma modu ve anomali araştırmasını geliştirmek');
  const [dataset, setDataset] = useState('');
  const [runId, setRunId] = useState('');
  const [seconds, setSeconds] = useState(1800);
  const [tokens, setTokens] = useState(30000);
  const [ram, setRam] = useState(8);
  const [vram, setVram] = useState(12);
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const currentRun = runs.find(run => run.run_id === runId);
  const capacity = observedCapacity(system);
  const ramMax = capacity.ram_gib === null ? undefined : Math.floor(capacity.ram_gib);
  const vramMax = capacity.vram_gib === null ? undefined : Math.floor(capacity.vram_gib);
  useEffect(() => {
    if (ramMax != null && ramMax >= 1) setRam(value => Math.min(value, ramMax));
    if (vramMax != null && vramMax >= 1) setVram(value => Math.min(value, vramMax));
    setDraft(null);
  }, [ramMax, vramMax]);
  useEffect(() => {
    setDraft(null);
    if (experience) setRunId(runs.some(run => run.run_id === experience.run_id) ? experience.run_id : '');
  }, [experience?.run_id, experience?.report_sha256]);
  function exportDraft(event: React.FormEvent) {
    event.preventDefault(); setError(null); setDraft(null);
    if (!teacher.trim() || !student.trim() || !objective.trim() || (!dataset.trim() && !currentRun && !experience)) { setError('Yerel model, hedef, amaç ve veri veya doğrulanmış rapor referansı gerekli.'); return; }
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(teacher.trim())) { setError('Öğretmen alanına dış servis adresi yerine yerel model kimliği veya profil referansı girin.'); return; }
    if (!Number.isInteger(seconds) || seconds < 1 || seconds > 14400 || !Number.isInteger(tokens) || tokens < 1 || tokens > 350000 || !Number.isInteger(ram) || ram < 1 || (ramMax != null && ram > ramMax) || !Number.isInteger(vram) || vram < 1 || (vramMax != null && vram > vramMax)) { setError('Bütçeler pozitif tam sayı olmalı; bellek önerisi bilinen host toplamını aşamaz.'); return; }
    const payload = {
      schema: 'agent-teaching-draft.v1', status: 'draft', created_at: new Date().toISOString(), training_started: false,
      teacher: { model_reference: teacher.trim(), runtime: 'local', profile_verified: false },
      student_reference: student.trim(), purpose, objective: objective.trim(),
      data: { reference: dataset.trim() || null, run_id: currentRun?.run_id ?? experience?.run_id ?? null, report_sha256: currentRun?.report_sha256 ?? experience?.report_sha256 ?? null, training_eligibility: 'unreviewed', usage_profile: 'noncommercial_research', raw_data_included: false, holdout_allowed: false },
      experience_preparation: experience ? { run_id: experience.run_id, report_sha256: experience.report_sha256, verification: experience.verification, eligible_training_records: 0, records: experience.records.map(record => ({ record_id: record.record_id, experiment_sha256: record.experiment_sha256 ?? null, trajectory_sha256: record.trajectory_sha256 ?? null, training_eligibility: record.training_eligibility })) } : null,
      budget: { wall_seconds: seconds, model_tokens: tokens, cpu_threads: 2, ram_gib: ram, vram_gib: vram },
      observed_capacity: { ...capacity, observed_at: observedAt ?? null },
      resource_policy: { shared_with_aos: true, admission_required: true, bounded_calls_and_yield_required: true, gpu_reserved: false },
      evaluation: { independent_comparison_required: true, holdout_required: true, rollback_required: true, auto_activate: false },
    };
    const value = JSON.stringify(payload, null, 2);
    const url = URL.createObjectURL(new Blob([value], { type: 'application/json' }));
    const link = document.createElement('a'); link.href = url; link.download = 'agent-teaching-draft.json'; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000); setDraft(value);
  }
  return <section className="panel agent-teacher" id="teacher-draft" aria-labelledby="teacher-draft-title"><div className="panel-heading"><div><h2 id="teacher-draft-title">Öğretmen model ve eğitim taslağı</h2><p>İhtiyaca göre yerel öğretmen ve hedef yeteneği tanımlayın. Bu form model veya eğitim işi çalıştırmaz.</p></div><span className="agent-label waiting">Eğitim henüz çalıştırılmadı</span></div>
    <form className="run-form agent-draft-form" onSubmit={exportDraft}>
      {experience && <p className="agent-note agent-draft-full">Seçilen doğrulanmış deney hafızası: {experience.run_id} · {experience.records.length} kayıt. Hazırlık referansları taslağa eklenir; eğitim uygunluğu henüz verilmedi.</p>}
      <label>Yerel öğretmen model<input required maxLength={160} value={teacher} onChange={e => { setTeacher(e.target.value); setDraft(null); }}/></label>
      <label>Hedef eylemci / model<input required maxLength={160} value={student} onChange={e => { setStudent(e.target.value); setDraft(null); }}/></label>
      <label>Eğitim amacı<select value={purpose} onChange={e => { setPurpose(e.target.value); setDraft(null); }}><option value="teacher_examples">Öğretmen örnekleri hazırlama</option><option value="sft_data">SFT veri hazırlığı</option><option value="lora_evaluation">LoRA değerlendirme planı</option><option value="qlora_evaluation">QLoRA değerlendirme planı</option></select></label>
      <label>Geliştirilecek yetenek<input required maxLength={600} value={objective} onChange={e => { setObjective(e.target.value); setDraft(null); }}/></label>
      <label>Veri manifesti / snapshot referansı<input maxLength={256} value={dataset} onChange={e => { setDataset(e.target.value); setDraft(null); }} placeholder="İzinleri incelenecek veri referansı"/></label>
      <label>Mevcut rapor referansı<select value={runId} onChange={e => { setRunId(e.target.value); setDraft(null); }}><option value="">İsteğe bağlı · tamamlanmış koşu seçin</option>{runs.map(run => <option key={run.run_id} value={run.run_id}>{run.run_id}</option>)}</select></label>
      <label>Süre üst sınırı (saniye)<input type="number" required min="1" max="14400" value={seconds} onChange={e => { setSeconds(Number(e.target.value)); setDraft(null); }}/></label>
      <label>Model token üst sınırı<input type="number" required min="1" max="350000" value={tokens} onChange={e => { setTokens(Number(e.target.value)); setDraft(null); }}/></label>
      <label>RAM bütçesi (GiB)<input type="number" required min="1" max={ramMax} value={ram} onChange={e => { setRam(Number(e.target.value)); setDraft(null); }}/></label>
      <label>VRAM bütçesi (GiB)<input type="number" required min="1" max={vramMax} value={vram} onChange={e => { setVram(Number(e.target.value)); setDraft(null); }}/></label>
      <p className="agent-note agent-draft-full">Taslak yalnız referansları içerir. Veri/model lisansı, kayıt temizliği, eğitim uygunluğu ve yerel model kapasitesi incelenmedi. Holdout verisi eğitimden dışlanır. Bütçe kaynak ayırmaz; paylaşımlı host kabulü ayrıca gerekir.</p>
      {error && <p className="field-error agent-draft-full" role="alert">{error}</p>}
      <div className="agent-actions agent-draft-full"><button className="button primary" type="submit">Eğitim taslağı JSON indir</button>{trainingEvidence && <button className="button secondary" type="button" onClick={() => onEvidence(trainingEvidence)}>Eğitim kabulünü incele</button>}</div>
    </form>
    {draft && <div className="agent-draft-result" role="status"><strong>Taslak hazırlandı; eğitim çalıştırılmadı.</strong><p>GPU ayırma, model çağrısı ve yeni eylemci sürümü oluşturma yapılmadı.</p><details><summary>İndirilen taslağı incele</summary><pre>{draft}</pre></details></div>}
  </section>;
}
