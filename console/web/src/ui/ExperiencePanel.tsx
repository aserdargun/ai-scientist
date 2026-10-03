import { useEffect, useState } from 'react';
import { api } from './api';
import type { PriorExperience, Run, RunExperience } from './types';

const terminal = new Set(['completed', 'stopped', 'failed']);
const stateNames: Record<string, string> = { completed: 'Tamamlandı', stopped: 'Durduruldu', failed: 'Başarısız', scored: 'Ölçüldü', crashed: 'Çalıştırma hatası', abandoned: 'Sonlandırıldı', rejected: 'Reddedildi' };
const kindNames: Record<string, string> = { baseline: 'CPU başlangıç ölçümü', proposal: 'Aday deneyi', 'cpu-mode-grid': 'CPU yöntem karşılaştırması', 'cpu-mode-stream': 'CPU OMR tanısı · puanlanmaz' };
const reasons: Record<string, string> = {
  'source-request-not-verified': 'Kaynak koşunun isteği doğrulanmadı',
  'not-measured-guarded-dev-proposal': 'Ölçülmüş ve kontrolleri geçmiş geliştirme adayı değil',
  'provenance-not-approved-for-history': 'Veri kökeni geçmiş bulgu kullanımına uygun değil',
  'method-not-approved-for-history': 'Yöntem geçmiş bulgu kullanımına uygun değil',
  'dev-replay-coverage-unavailable': 'Geliştirme tekrar ölçümlerinin kapsam kanıtı eksik',
  'dev-profile-provenance-coverage-unavailable': 'Geliştirme profili ve veri kökeni kapsam kanıtı eksik',
  'history-proof-unavailable': 'Geçmiş bulgu kullanımının kanıtı tamamlanmadı',
  'not_proposal': 'Öğretmen önerisi değil', 'not_local_llm': 'Yerel LLM örneği değil',
  'not_confirmed_positive_candidate': 'Teyit edilmiş olumlu aday değil',
  'missing_cleaning_and_runtime_review': 'Temizleme ve runtime incelemesi eksik',
  'dataset_training_permission_missing': 'Veri eğitim izni doğrulanmadı',
  'model_training_permission_missing': 'Model eğitim izni doğrulanmadı',
  'code_training_permission_missing': 'Kod eğitim izni doğrulanmadı',
  'protected_split': 'Holdout eğitimden dışlanır',
  'referee-keep': 'Referee geliştirme kümesinde KEEP kararı verdi',
  'referee-keep_simpler': 'Referee daha basit adayı seçti',
  'referee-discard': 'Referee adayı seçmedi (DISCARD)',
  'referee-reject': 'Referee adayı reddetti (REJECT)',
  'referee-drop': 'Referee adayı seçmedi',
  'cpu-dataset-evidence': 'CPU veri kümesi ölçümü',
  'export-permission-not-checked': 'Veri, model ve kodun eğitim izinleri incelenmedi',
  'clean-runtime-review-not-checked': 'Runtime kanıtı ve kayıt temizliği incelenmedi',
  'cpu-not-local-llm': 'CPU ölçümü; yerel LLM eğitim örneği değil',
  'model-receipt-fixture-or-unverified': 'Fixture veya doğrulanmamış model makbuzu',
  'model-receipt-absent': 'Model makbuzu yok',
  'model-receipt-invalid': 'Model makbuzu doğrulanamadı',
  'model-receipt-identity-mismatch': 'Model makbuzunun kimliği eşleşmiyor',
  'protected-provenance-withheld': 'Korumalı kaynak bilgisi bu görünümden çıkarıldı',
  'dataset-provenance-unavailable': 'Veri kaynağının köken kanıtı yok',
  'no-experiment-trajectory': 'Puanlanmayan tanı raporu; deney trajectory kaydı yok',
};
const reasonText = (code: string) => reasons[code] ?? code;
const receiptNames: Record<string, string> = {
  'not-applicable-cpu': 'CPU işi; model çağrısı yok',
  'fixture-or-unverified': 'Fixture veya doğrulanmamış model kaydı',
  absent: 'Model makbuzu yok', invalid: 'Model makbuzu doğrulanamadı',
  'identity-mismatch': 'Model kimlikleri eşleşmiyor',
  'reported-local-identity-matched': 'Bildirilen yerel model kimliği eşleşti; runtime incelemesi ayrı',
};

const object = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);
const uuid = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
const hash = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
function validExperience(value: unknown, runId: string, reportHash: string): value is RunExperience {
  if (!object(value) || value.schema !== 'run-experience.v1' || value.run_id !== runId
    || value.report_sha256 !== reportHash || !hash(value.report_sha256)
    || value.verification !== 'report-and-ledger-hash-verified'
    || typeof value.purpose !== 'string' || value.training_started !== false || value.holdout_included !== false
    || (value.protected_records_excluded != null && typeof value.protected_records_excluded !== 'boolean')
    || !object(value.feedback) || value.feedback.cross_run_reuse !== false || value.feedback.same_run_recent_limit !== 30
    || typeof value.record_limit !== 'number' || !Number.isInteger(value.record_limit) || value.record_limit < 1 || value.record_limit > 200
    || !Array.isArray(value.records) || value.records.length > value.record_limit) return false;
  const field = value.field_context_usage;
  if (field != null) {
    if (!object(field) || !hash(field.field_context_sha256) || !hash(field.snapshot_sha256)
      || field.asset_identity !== 'user_supplied' || field.use !== 'advisory-only'
      || !object(field.intent) || typeof field.intent.goal_kind !== 'string' || !['digital_twin', 'predictive_maintenance'].includes(field.intent.goal_kind)
      || ![['asset_id', 128], ['objective', 600]].every(([key, limit]) => {
        const text = (field.intent as Record<string, unknown>)[key as string];
        return typeof text === 'string' && text === text.trim() && Array.from(text).length >= 1
          && Array.from(text).length <= (limit as number) && !/[\u0000-\u001f\u007f]/.test(text);
      })
      || typeof field.status !== 'string' || !['admitted-only', 'context-bound'].includes(field.status)
      || typeof field.context_bound_proposal_count !== 'number' || !Number.isSafeInteger(field.context_bound_proposal_count)
      || (field.status === 'admitted-only' ? field.context_bound_proposal_count !== 0 : field.context_bound_proposal_count < 1)) return false;
  }
  const usage = value.prior_findings_usage;
  if (usage != null && (!object(usage) || !hash(usage.snapshot_sha256) || !uuid(usage.source_run_id)
    || !hash(usage.source_report_sha256) || typeof usage.record_count !== 'number' || !Number.isSafeInteger(usage.record_count)
    || usage.record_count < 1 || usage.record_count > 8 || typeof usage.context_bound_proposal_count !== 'number'
    || !Number.isSafeInteger(usage.context_bound_proposal_count) || usage.context_bound_proposal_count < 0
    || usage.scope !== 'historical-advisory-only'
    || typeof usage.status !== 'string' || !['admitted-only', 'context-bound'].includes(usage.status)
    || (usage.status === 'admitted-only' ? usage.context_bound_proposal_count !== 0 : usage.context_bound_proposal_count < 1))) return false;
  const ids = new Set<string>();
  const selectedExperimentIds = new Set<string>();
  return value.records.every(record => {
    if (!object(record)) return false;
    if (typeof record.record_id !== 'string' || ids.has(record.record_id)) return false;
    ids.add(record.record_id);
    const history = record.history_eligibility;
    if (history != null && (!object(history) || typeof history.eligible !== 'boolean' || !Array.isArray(history.reasons)
      || !history.reasons.every(reason => typeof reason === 'string')
      || (history.eligible && (record.kind !== 'proposal' || record.status !== 'scored' || record.score_kind !== 'dev-suite'
        || typeof record.score !== 'number' || !Number.isFinite(record.score) || !(typeof record.experiment_id === 'string' && /^exp_[0-9a-f]{32}$/.test(record.experiment_id))
        || !hash(record.experiment_sha256) || !hash(record.trajectory_sha256))))) return false;
    if (object(history) && history.eligible) {
      const experimentId = record.experiment_id as string;
      if (selectedExperimentIds.has(experimentId)) return false;
      selectedExperimentIds.add(experimentId);
    }
    const eligibility = record.training_eligibility;
    const provenance = record.provenance_summary;
    return typeof record.record_id === 'string' && record.record_id.length > 0
      && typeof record.sequence === 'number' && Number.isSafeInteger(record.sequence) && record.sequence >= 0
      && typeof record.kind === 'string' && typeof record.status === 'string'
      && typeof record.model_receipt_status === 'string'
      && ['experiment_id', 'method', 'move', 'decision', 'reason', 'model_id'].every(key => record[key] == null || typeof record[key] === 'string')
      && (record.score_kind == null || (typeof record.score_kind === 'string' && ['dev-suite', 'withheld', 'not-scored'].includes(record.score_kind)))
      && ['experiment_sha256', 'trajectory_sha256'].every(key => record[key] == null || hash(record[key]))
      && (record.score == null || (typeof record.score === 'number' && Number.isFinite(record.score)))
      && object(eligibility) && eligibility.eligible === false && Array.isArray(eligibility.reasons)
      && eligibility.reasons.every(reason => typeof reason === 'string')
      && object(provenance) && typeof provenance.source_count === 'number'
      && Number.isSafeInteger(provenance.source_count) && provenance.source_count >= 0
      && Array.isArray(provenance.manifest_sha256s) && provenance.manifest_sha256s.every(hash)
      && typeof provenance.usage_profile === 'string';
  });
}

export function downloadPreparation(experience: RunExperience) {
  const payload = {
    schema: 'agent-teacher-data-preparation.v1', created_at: new Date().toISOString(),
    status: 'review_required', training_started: false, holdout_included: false,
    usage_profile: 'noncommercial_research', raw_data_included: false, messages_included: false,
    run_id: experience.run_id, report_sha256: experience.report_sha256,
    verification: experience.verification, eligible_training_records: 0,
    protected_records_excluded: experience.protected_records_excluded ?? null,
    records: experience.records.map(record => ({
      record_id: record.record_id, kind: record.kind, experiment_id: record.experiment_id ?? null,
      experiment_sha256: record.experiment_sha256 ?? null,
      trajectory_sha256: record.trajectory_sha256 ?? null,
      source_manifest_sha256s: record.provenance_summary.manifest_sha256s,
      training_eligibility: record.training_eligibility,
    })),
    requirements: ['review_runtime_and_cleaning', 'review_data_model_code_training_permissions', 'local_teacher_only', 'independent_evaluation_before_promotion'],
    auto_activate: false,
  };
  const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }));
  const link = document.createElement('a'); link.href = url;
  link.download = `teacher-data-preparation-${experience.run_id}.json`; link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function ExperiencePanel({ runs, connected, onVerifiedChange, onReport, onDesign }: {
  runs: Run[]; connected: boolean; onVerifiedChange: (data: RunExperience | null) => void;
  onReport: (id: string) => void; onDesign: (prior: PriorExperience) => void;
}) {
  const [picked, setPicked] = useState<string[]>([]);
  const [runId, setRunId] = useState('');
  const [data, setData] = useState<RunExperience | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(0);
  const [showAll, setShowAll] = useState(false);
  const [downloaded, setDownloaded] = useState(false);
  const choices = runs.filter(run => !run.stale && !run.unavailable && terminal.has(run.state) && run.report_sha256);
  const selected = choices.find(run => run.run_id === runId);
  const selectedHash = selected?.report_sha256;
  const available = connected && !!selected;
  const current = available && data?.run_id === runId && data.report_sha256 === selectedHash ? data : null;
  const orderedRecords = current ? [...current.records].sort((a, b) => Number(b.kind === 'proposal') - Number(a.kind === 'proposal')) : [];
  useEffect(() => {
    const controller = new AbortController();
    setPicked([]); setData(null); setError(null); setDownloaded(false); setShowAll(false); onVerifiedChange(null);
    if (!available || !selectedHash) { setBusy(false); return () => controller.abort(); }
    setBusy(true);
    void api.experience(runId, controller.signal).then(result => {
      if (controller.signal.aborted) return;
      if (!validExperience(result, runId, selectedHash)) {
        throw new Error('Deney hafızasının kimliği veya doğrulama bilgisi seçilen raporla eşleşmiyor.');
      }
      setData(result); onVerifiedChange(result);
    }).catch(cause => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Deney hafızası okunamadı.');
    }).finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [runId, selectedHash, available, retry, onVerifiedChange]);
  return <section className="panel agent-experience" id="agent-experience" aria-labelledby="experience-title">
    <div className="panel-heading"><div><h2 id="experience-title">Doğrulanmış deney hafızası</h2><p>Tamamlanmış, durdurulmuş veya başarısız bir koşu seçin; yalnız o koşunun rapora bağlı kayıtları okunur.</p></div><span className="agent-label waiting">Öğrenilmiş adapter yok</span></div>
    <div className="run-form experience-selection"><label>Deney hafızası için koşu<select value={runId} onChange={event => { setRunId(event.target.value); setPicked([]); setData(null); setDownloaded(false); onVerifiedChange(null); }} disabled={!connected || !choices.length}><option value="">Koşu seçin</option>{choices.map(run => <option value={run.run_id} key={run.run_id}>{run.run_id} · {stateNames[run.state] ?? run.state}</option>)}</select></label></div>
    {!connected ? <p className="agent-note">Lab bağlantısı yok; son kayıtlar güncelmiş gibi gösterilmez.</p> : !choices.length ? <div className="empty-state"><strong>Sonuç raporu olan koşu yok</strong><span>Deneyler ekranında mevcut koşuyu izlemeye ekleyin. Hafıza örnek kayıtlarla doldurulmaz.</span></div> : !runId ? <p className="agent-note">Seçim yapılana kadar deney hafızası yüklenmez.</p> : !selected ? <p className="agent-note" role="status">Seçilen koşu güncel okunamadı. Yeniden geçerli bir koşu seçin.</p> : busy ? <p role="status">Deney hafızası doğrulanıyor…</p> : error ? <div role="alert"><p className="field-error">{error}</p><button className="button secondary small" onClick={() => setRetry(value => value + 1)}>Yeniden dene</button></div> : null}
    {current && <>
      <div className="agent-actions experience-summary"><span className="agent-label available">Rapor ve kayıt hashleri doğrulandı</span><span>{current.records.length} deney kaydı</span><button className="button secondary small" onClick={() => onReport(current.run_id)}>Bağımsız raporu aç</button><button className="button primary" onClick={() => { downloadPreparation(current); setDownloaded(true); }} disabled={!current.records.length}>Öğretmen veri hazırlığı JSON indir</button><a className="button secondary" href="#teacher-draft">Öğretmen ve bütçe seç</a></div>
      {downloaded && <p role="status">Veri hazırlık manifesti indirildi; eğitim çalıştırılmadı.</p>}
      <div className="experience-handoff"><strong>Geçmiş bulgularla yeni deney</strong><p>Sunucunun uygun bulduğu geliştirme kayıtlarını açıkça seçin (en fazla 8). Yalnız doğrulanmış referanslar taşınır. Geçmiş skorlar danışma içindir; yeni Scorer ölçümü, KEEP kararı veya eğitim izni değildir.</p><div className="agent-actions"><span>{picked.length}/8 kayıt seçildi</span><button className="button primary" disabled={!picked.length} onClick={() => onDesign({ source_run_id: current.run_id, source_report_sha256: current.report_sha256, records: current.records.filter(record => picked.includes(record.record_id) && record.history_eligibility?.eligible).map(record => ({ experiment_id: record.experiment_id!, experiment_sha256: record.experiment_sha256!, trajectory_sha256: record.trajectory_sha256! })) })}>Seçilenlerle deney tasarla</button><button className="button secondary small" disabled={!picked.length} onClick={() => setPicked([])}>Seçimi temizle</button></div></div>
      {current.field_context_usage && <div className="experience-handoff"><h3>{current.field_context_usage.status === 'context-bound' ? 'Saha amacı Director bağlamına taşındı' : 'Saha amacı kabul edildi; öneri bağlamı henüz yok'}</h3><dl><dt>Varlık etiketi · kullanıcı beyanı</dt><dd>{current.field_context_usage.intent.asset_id}</dd><dt>Araştırma amacı</dt><dd>{current.field_context_usage.intent.goal_kind === 'digital_twin' ? 'Dijital ikiz araştırması' : 'Kestirimci bakım araştırması'}</dd><dt>Çalışma hedefi</dt><dd>{current.field_context_usage.intent.objective}</dd></dl><p>{current.field_context_usage.context_bound_proposal_count} önerinin bağlamı doğrulandı. Bu amaç danışma bilgisidir; yetkili kaynak/varlık eşlemesi, bakım aksiyonu veya model iyileşmesi göstermez.</p><details><summary>Rapora bağlı saha amacı referansları</summary><dl><dt>Seçilen veri snapshot SHA-256</dt><dd>{current.field_context_usage.snapshot_sha256}</dd><dt>Saha bağlamı SHA-256</dt><dd>{current.field_context_usage.field_context_sha256}</dd></dl></details></div>}
      {current.prior_findings_usage && <div className="experience-handoff"><strong>{current.prior_findings_usage.status === 'context-bound' ? 'Seçili geçmiş bulgular Director bağlamına taşındı' : 'Geçmiş bulgular kabul edildi; öneri bağlamı henüz yok'}</strong><p>{current.prior_findings_usage.record_count} kayıt · {current.prior_findings_usage.context_bound_proposal_count} önerinin bağlamı doğrulandı. Bu kanıt model öğrenmesi veya iyileşme göstermez. Deterministik karşılaştırmanın parametre planı değişmez.</p><details><summary>Geçmiş bağlamın doğrulanmış referansları</summary><dl><dt>Kaynak koşu</dt><dd>{current.prior_findings_usage.source_run_id}</dd><dt>Kaynak rapor SHA-256</dt><dd>{current.prior_findings_usage.source_report_sha256}</dd><dt>Geçmiş bulgu snapshot SHA-256</dt><dd>{current.prior_findings_usage.snapshot_sha256}</dd></dl></details></div>}
      <p className="agent-note">{current.feedback.same_run_recent_limit} son geliştirme geri bildirimi aynı araştırma koşusunda sonraki öneriye taşınabilir. Yeni koşular arasında otomatik hafıza yeniden kullanımı, skill ve adapter terfisi henüz tamamlanmadı.</p>
      {current.protected_records_excluded && <p className="agent-note">Korumalı kaynaklı kayıtlar bu görünümden ve veri hazırlık manifestinden çıkarıldı.</p>}
      {!current.records.length && <p className="agent-note">Doğrulanmış raporda gösterilecek deney kaydı yok.</p>}
      <div className="experience-records">{orderedRecords.slice(0, showAll ? undefined : 20).map(record => <article className="experience-record" key={record.record_id}>
        <div className="experience-record-heading"><h3>{kindNames[record.kind] ?? record.kind}{record.method ? ` · ${record.method.toUpperCase()}` : ''}</h3><span className={`agent-label ${record.decision === 'KEEP' || record.decision === 'KEEP_SIMPLER' ? 'available' : 'waiting'}`}>{record.decision ?? stateNames[record.status] ?? record.status}</span></div>
        <p>{record.reason ? reasonText(record.reason) : 'Karar gerekçesi kaydı yok.'}{record.score != null ? ` Geliştirme skoru: ${record.score.toLocaleString('tr-TR', { maximumFractionDigits: 4 })}.` : record.score_kind === 'withheld' ? ' Korumalı kaynak skoru bu görünümde gösterilmez.' : ' Bu görünümde karşılaştırmalı skor yok.'}</p>
        <p className="agent-note">{record.model_id ? `Model: ${record.model_id}. ` : ''}{receiptNames[record.model_receipt_status] ?? record.model_receipt_status}</p>
        {record.history_eligibility?.eligible ? <label className="experience-pick"><input type="checkbox" checked={picked.includes(record.record_id)} disabled={!picked.includes(record.record_id) && picked.length >= 8} onChange={event => setPicked(values => event.target.checked ? [...values, record.record_id].slice(0, 8) : values.filter(id => id !== record.record_id))}/>Yeni deney için geçmiş bulgu olarak seç · eğitim izni değildir</label> : <details><summary>Geçmiş bulgu seçimi: uygun değil</summary><ul>{(record.history_eligibility?.reasons ?? ['Uygunluk henüz değerlendirilmedi']).map((reason, index) => <li key={`${reason}-${index}`}>{reasonText(reason)}</li>)}</ul></details>}
        <details className="experience-eligibility"><summary>Eğitim hazırlığı: inceleme gerekiyor · eğitime hazır değil</summary><ul>{record.training_eligibility.reasons.map((reason, index) => <li key={`${reason}-${index}`}>{reasonText(reason)}</li>)}</ul></details>
        <details><summary>Kayıt referansları</summary><dl><dt>Kayıt</dt><dd>{record.record_id}</dd>{record.experiment_sha256 && <><dt>Deney SHA-256</dt><dd>{record.experiment_sha256}</dd></>}{record.trajectory_sha256 && <><dt>Trajectory SHA-256</dt><dd>{record.trajectory_sha256}</dd></>}<dt>Kaynak</dt><dd>{record.provenance_summary.source_count} · {record.provenance_summary.usage_profile === 'noncommercial_research' ? 'Ticari olmayan araştırma' : record.provenance_summary.usage_profile}</dd></dl></details>
      </article>)}</div>
      {current.records.length > 20 && !showAll && <button className="button secondary small" onClick={() => setShowAll(true)}>Tüm {current.records.length} kaydı göster</button>}
      <p className="agent-note">İndirilen hazırlık manifesti yalnız doğrulanmış referansları ve ret gerekçelerini içerir. Mesajlar/veri değerleri yok; eğitim ve holdout dahil değil. Uygunluk incelemesi yapılmadan SFT paketi değildir.</p>
    </>}
  </section>;
}
