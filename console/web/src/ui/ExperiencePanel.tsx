import { useEffect, useState } from 'react';
import { t, locale } from './i18n';
import { api } from './api';
import type { PriorExperience, Run, RunExperience } from './types';

const terminal = new Set(['completed', 'stopped', 'failed']);
const stateNames = (): Record<string, string> => ({ completed: t("Completed", "Tamamlandı"), stopped: t("Stopped", "Durduruldu"), failed: t("Failed", "Başarısız"), scored: t("Measured", "Ölçüldü"), crashed: t("Execution error", "Çalıştırma hatası"), abandoned: t("Terminated", "Sonlandırıldı"), rejected: t("Rejected", "Reddedildi") });
const kindNames = (): Record<string, string> => ({ baseline: t("CPU baseline", "CPU başlangıç ölçümü"), proposal: t("Candidate experiment", "Aday deneyi"), 'cpu-mode-grid': t("CPU method comparison", "CPU yöntem karşılaştırması"), 'cpu-mode-stream': t("CPU OMR diagnostic · not scored", "CPU OMR tanısı · puanlanmaz") });
const reasons = (): Record<string, string> => ({
  'source-request-not-verified': t("The source run request was not verified", "Kaynak koşunun isteği doğrulanmadı"),
  'not-measured-guarded-dev-proposal': t("Not a measured development candidate that passed the guards", "Ölçülmüş ve kontrolleri geçmiş geliştirme adayı değil"),
  'provenance-not-approved-for-history': t("Data provenance is not approved for historical findings", "Veri kökeni geçmiş bulgu kullanımına uygun değil"),
  'method-not-approved-for-history': t("The method is not approved for historical findings", "Yöntem geçmiş bulgu kullanımına uygun değil"),
  'dev-replay-coverage-unavailable': t("Development replay coverage evidence is missing", "Geliştirme tekrar ölçümlerinin kapsam kanıtı eksik"),
  'dev-profile-provenance-coverage-unavailable': t("Development profile and data provenance coverage evidence is missing", "Geliştirme profili ve veri kökeni kapsam kanıtı eksik"),
  'history-proof-unavailable': t("Historical finding reuse evidence is incomplete", "Geçmiş bulgu kullanımının kanıtı tamamlanmadı"),
  'not_proposal': t("Not an experiment proposal", "Deney önerisi değil"), 'not_local_llm': t("Not a local LLM example", "Yerel LLM örneği değil"),
  'not_confirmed_positive_candidate': t("Not a confirmed positive candidate", "Teyit edilmiş olumlu aday değil"),
  'missing_cleaning_and_runtime_review': t("Cleaning and runtime review are missing", "Temizleme ve runtime incelemesi eksik"),
  'dataset_training_permission_missing': t("Data training permission was not verified", "Veri eğitim izni doğrulanmadı"),
  'model_training_permission_missing': t("Model training permission was not verified", "Model eğitim izni doğrulanmadı"),
  'code_training_permission_missing': t("Code training permission was not verified", "Kod eğitim izni doğrulanmadı"),
  'protected_split': t("Holdout is excluded from training", "Holdout eğitimden dışlanır"),
  'referee-keep': t("Referee selected KEEP on the development set", "Referee geliştirme kümesinde KEEP kararı verdi"),
  'referee-keep_simpler': t("Referee selected the simpler candidate", "Referee daha basit adayı seçti"),
  'referee-discard': t("Referee did not select the candidate (DISCARD)", "Referee adayı seçmedi (DISCARD)"),
  'referee-reject': t("Referee rejected the candidate (REJECT)", "Referee adayı reddetti (REJECT)"),
  'referee-drop': t("Referee did not select the candidate", "Referee adayı seçmedi"),
  'cpu-dataset-evidence': t("CPU dataset measurement", "CPU veri kümesi ölçümü"),
  'export-permission-not-checked': t("Data, model and code training permissions have not been reviewed", "Veri, model ve kodun eğitim izinleri incelenmedi"),
  'clean-runtime-review-not-checked': t("Runtime evidence and record cleaning have not been reviewed", "Runtime kanıtı ve kayıt temizliği incelenmedi"),
  'cpu-not-local-llm': t("CPU measurement; not a local LLM training example", "CPU ölçümü; yerel LLM eğitim örneği değil"),
  'model-receipt-fixture-or-unverified': t("Fixture or unverified model receipt", "Fixture veya doğrulanmamış model makbuzu"),
  'model-receipt-absent': t("No model receipt", "Model makbuzu yok"),
  'model-receipt-invalid': t("Model receipt could not be verified", "Model makbuzu doğrulanamadı"),
  'model-receipt-identity-mismatch': t("Model receipt identity mismatch", "Model makbuzunun kimliği eşleşmiyor"),
  'protected-provenance-withheld': t("Protected provenance was omitted from this view", "Korumalı kaynak bilgisi bu görünümden çıkarıldı"),
  'dataset-provenance-unavailable': t("Data provenance evidence is unavailable", "Veri kaynağının köken kanıtı yok"),
  'no-experiment-trajectory': t("Unscored diagnostic report; no experiment trajectory recorded", "Puanlanmayan tanı raporu; deney trajectory kaydı yok"),
});
const reasonText = (code: string) => reasons()[code] ?? code;
const receiptNames = (): Record<string, string> => ({
  'not-applicable-cpu': t("CPU run; no model calls", "CPU işi; model çağrısı yok"),
  'fixture-or-unverified': t("Fixture or unverified model record", "Fixture veya doğrulanmamış model kaydı"),
  absent: t("No model receipt", "Model makbuzu yok"), invalid: t("Model receipt could not be verified", "Model makbuzu doğrulanamadı"),
  'identity-mismatch': t("Model identities do not match", "Model kimlikleri eşleşmiyor"),
  'reported-local-identity-matched': t("Reported local model identity matched; runtime inspection is separate", "Bildirilen yerel model kimliği eşleşti; runtime incelemesi ayrı"),
});

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
    schema: 'agent-teacher-data-preparation.v2', created_at: new Date().toISOString(),
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
    requirements: ['review_runtime_and_cleaning', 'review_data_model_code_training_permissions', 'review_teacher_provider_and_explicit_external_request_approval', 'independent_evaluation_before_promotion'],
    optional_teacher: { provider: 'unselected', external_api_requires_explicit_approval: true, approved: false, executed: false },
    main_agent_runtime: 'local', auto_activate: false,
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
        throw new Error('experience_identity_mismatch');
      }
      setData(result); onVerifiedChange(result);
    }).catch(cause => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'experience_unavailable');
    }).finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [runId, selectedHash, available, retry, onVerifiedChange]);
  return <section className="panel agent-experience" id="agent-experience" aria-labelledby="experience-title">
    <div className="panel-heading"><div><h2 id="experience-title">{t("Verified experiment memory", "Doğrulanmış deney hafızası")}</h2><p>{t("Select a completed, stopped or failed run; only its report-bound records are read.", "Tamamlanmış, durdurulmuş veya başarısız bir koşu seçin; yalnız o koşunun rapora bağlı kayıtları okunur.")}</p></div><span className="agent-label waiting">{t("No trained adapter", "Öğrenilmiş adapter yok")}</span></div>
    <div className="run-form experience-selection"><label>{t("Run for experiment memory", "Deney hafızası için koşu")}<select value={runId} onChange={event => { setRunId(event.target.value); setPicked([]); setData(null); setDownloaded(false); onVerifiedChange(null); }} disabled={!connected || !choices.length}><option value="">{t("Select a run", "Koşu seçin")}</option>{choices.map(run => <option value={run.run_id} key={run.run_id}>{run.run_id} · {stateNames()[run.state] ?? run.state}</option>)}</select></label></div>
    {!connected ? <p className="agent-note">{t("Lab is disconnected; previous records are not presented as current.", "Lab bağlantısı yok; son kayıtlar güncelmiş gibi gösterilmez.")}</p> : !choices.length ? <div className="empty-state"><strong>{t("No runs with a result report", "Sonuç raporu olan koşu yok")}</strong><span>{t("Track an existing run from the Experiments screen. Memory is not populated with sample records.", "Deneyler ekranında mevcut koşuyu izlemeye ekleyin. Hafıza örnek kayıtlarla doldurulmaz.")}</span></div> : !runId ? <p className="agent-note">{t("Experiment memory is not loaded until a run is selected.", "Seçim yapılana kadar deney hafızası yüklenmez.")}</p> : !selected ? <p className="agent-note" role="status">{t("The selected run could not be refreshed. Select a valid run again.", "Seçilen koşu güncel okunamadı. Yeniden geçerli bir koşu seçin.")}</p> : busy ? <p role="status">{t("Verifying experiment memory…", "Deney hafızası doğrulanıyor…")}</p> : error ? <div role="alert"><p className="field-error">{error === 'experience_identity_mismatch' ? t('Experiment memory identity or verification does not match the selected report.', 'Deney hafızasının kimliği veya doğrulama bilgisi seçilen raporla eşleşmiyor.') : error === 'experience_unavailable' ? t('Experiment memory could not be read.', 'Deney hafızası okunamadı.') : error}</p><button className="button secondary small" onClick={() => setRetry(value => value + 1)}>{t("Retry", "Yeniden dene")}</button></div> : null}
    {current && <>
      <div className="agent-actions experience-summary"><span className="agent-label available">{t("Report and record hashes verified", "Rapor ve kayıt hashleri doğrulandı")}</span><span>{current.records.length} {t(" experiment records", " deney kaydı")}</span><button className="button secondary small" onClick={() => onReport(current.run_id)}>{t("Open independent report", "Bağımsız raporu aç")}</button><button className="button secondary" onClick={() => { downloadPreparation(current); setDownloaded(true); }} disabled={!current.records.length}>{t("Download teacher data preparation JSON", "Öğretmen veri hazırlığı JSON indir")}</button><a className="button secondary" href="#teacher-draft">{t("Optional teacher draft", "İsteğe bağlı öğretmen taslağı")}</a></div>
      {downloaded && <p role="status">{t("Data preparation manifest downloaded; training was not run.", "Veri hazırlık manifesti indirildi; eğitim çalıştırılmadı.")}</p>}
      <div className="experience-handoff"><strong>{t("New experiment with historical findings", "Geçmiş bulgularla yeni deney")}</strong><p>{t("Explicitly select development records approved by the server (up to 8). Only verified references are carried forward. Historical scores are advisory; they are not a new Scorer measurement, KEEP decision or training permission.", "Sunucunun uygun bulduğu geliştirme kayıtlarını açıkça seçin (en fazla 8). Yalnız doğrulanmış referanslar taşınır. Geçmiş skorlar danışma içindir; yeni Scorer ölçümü, KEEP kararı veya eğitim izni değildir.")}</p><div className="agent-actions"><span>{picked.length}{t("/8 records selected", "/8 kayıt seçildi")}</span><button className="button primary" disabled={!picked.length} onClick={() => onDesign({ source_run_id: current.run_id, source_report_sha256: current.report_sha256, records: current.records.filter(record => picked.includes(record.record_id) && record.history_eligibility?.eligible).map(record => ({ experiment_id: record.experiment_id!, experiment_sha256: record.experiment_sha256!, trajectory_sha256: record.trajectory_sha256! })) })}>{t("Design experiment with selection", "Seçilenlerle deney tasarla")}</button><button className="button secondary small" disabled={!picked.length} onClick={() => setPicked([])}>{t("Clear selection", "Seçimi temizle")}</button></div></div>
      {current.field_context_usage && <div className="experience-handoff"><h3>{current.field_context_usage.status === 'context-bound' ? t("Field intent carried into the Director context", "Saha amacı Director bağlamına taşındı") : t("Field intent admitted; no proposal context yet", "Saha amacı kabul edildi; öneri bağlamı henüz yok")}</h3><dl><dt>{t("Asset label · user declaration", "Varlık etiketi · kullanıcı beyanı")}</dt><dd>{current.field_context_usage.intent.asset_id}</dd><dt>{t("Research goal", "Araştırma amacı")}</dt><dd>{current.field_context_usage.intent.goal_kind === 'digital_twin' ? t("Digital twin research", "Dijital ikiz araştırması") : t("Predictive maintenance research", "Kestirimci bakım araştırması")}</dd><dt>{t("Working objective", "Çalışma hedefi")}</dt><dd>{current.field_context_usage.intent.objective}</dd></dl><p>{current.field_context_usage.context_bound_proposal_count} {t(" proposal contexts verified. This intent is advisory; it does not demonstrate an authorized source/asset mapping, maintenance action or model improvement.", " önerinin bağlamı doğrulandı. Bu amaç danışma bilgisidir; yetkili kaynak/varlık eşlemesi, bakım aksiyonu veya model iyileşmesi göstermez.")}</p><details><summary>{t("Report-bound field intent references", "Rapora bağlı saha amacı referansları")}</summary><dl><dt>{t("Selected data snapshot SHA-256", "Seçilen veri snapshot SHA-256")}</dt><dd>{current.field_context_usage.snapshot_sha256}</dd><dt>{t("Field context SHA-256", "Saha bağlamı SHA-256")}</dt><dd>{current.field_context_usage.field_context_sha256}</dd></dl></details></div>}
      {current.prior_findings_usage && <div className="experience-handoff"><strong>{current.prior_findings_usage.status === 'context-bound' ? t("Selected historical findings carried into the Director context", "Seçili geçmiş bulgular Director bağlamına taşındı") : t("Historical findings admitted; no proposal context yet", "Geçmiş bulgular kabul edildi; öneri bağlamı henüz yok")}</strong><p>{current.prior_findings_usage.record_count} {t(" records · ", " kayıt · ")}{current.prior_findings_usage.context_bound_proposal_count} {t(" proposal contexts verified. This evidence does not demonstrate model learning or improvement. The parameter plan for deterministic comparison is unchanged.", " önerinin bağlamı doğrulandı. Bu kanıt model öğrenmesi veya iyileşme göstermez. Deterministik karşılaştırmanın parametre planı değişmez.")}</p><details><summary>{t("Verified historical context references", "Geçmiş bağlamın doğrulanmış referansları")}</summary><dl><dt>{t("Source run", "Kaynak koşu")}</dt><dd>{current.prior_findings_usage.source_run_id}</dd><dt>{t("Source report SHA-256", "Kaynak rapor SHA-256")}</dt><dd>{current.prior_findings_usage.source_report_sha256}</dd><dt>{t("Historical finding snapshot SHA-256", "Geçmiş bulgu snapshot SHA-256")}</dt><dd>{current.prior_findings_usage.snapshot_sha256}</dd></dl></details></div>}
      <p className="agent-note">{current.feedback.same_run_recent_limit} {t(" recent development feedback summaries may be carried into the next proposal in the same research run. Automatic memory reuse across new runs, skills and adapter promotion are not yet complete.", " son geliştirme geri bildirimi aynı araştırma koşusunda sonraki öneriye taşınabilir. Yeni koşular arasında otomatik hafıza yeniden kullanımı, skill ve adapter terfisi henüz tamamlanmadı.")}</p>
      {current.protected_records_excluded && <p className="agent-note">{t("Records from protected sources were excluded from this view and the data preparation manifest.", "Korumalı kaynaklı kayıtlar bu görünümden ve veri hazırlık manifestinden çıkarıldı.")}</p>}
      {!current.records.length && <p className="agent-note">{t("The verified report has no experiment records to display.", "Doğrulanmış raporda gösterilecek deney kaydı yok.")}</p>}
      <div className="experience-records">{orderedRecords.slice(0, showAll ? undefined : 20).map(record => <article className="experience-record" key={record.record_id}>
        <div className="experience-record-heading"><h3>{kindNames()[record.kind] ?? record.kind}{record.method ? ` · ${record.method.toUpperCase()}` : ''}</h3><span className={`agent-label ${record.decision === 'KEEP' || record.decision === 'KEEP_SIMPLER' ? 'available' : 'waiting'}`}>{record.decision ?? stateNames()[record.status] ?? record.status}</span></div>
        <p>{record.reason ? reasonText(record.reason) : t("No decision reason recorded.", "Karar gerekçesi kaydı yok.")}{record.score != null ? t(` Development score: ${record.score.toLocaleString(locale(), { maximumFractionDigits: 4 })}.`, ` Geliştirme skoru: ${record.score.toLocaleString(locale(), { maximumFractionDigits: 4 })}.`) : record.score_kind === 'withheld' ? t(" The protected source score is not shown in this view.", " Korumalı kaynak skoru bu görünümde gösterilmez.") : t(" No comparative score is available in this view.", " Bu görünümde karşılaştırmalı skor yok.")}</p>
        <p className="agent-note">{record.model_id ? t(`Model: ${record.model_id}. `, `Model: ${record.model_id}. `) : ''}{receiptNames()[record.model_receipt_status] ?? record.model_receipt_status}</p>
        {record.history_eligibility?.eligible ? <label className="experience-pick"><input type="checkbox" checked={picked.includes(record.record_id)} disabled={!picked.includes(record.record_id) && picked.length >= 8} onChange={event => setPicked(values => event.target.checked ? [...values, record.record_id].slice(0, 8) : values.filter(id => id !== record.record_id))}/>{t("Select as a historical finding for a new experiment · not training permission", "Yeni deney için geçmiş bulgu olarak seç · eğitim izni değildir")}</label> : <details><summary>{t("Historical finding selection: ineligible", "Geçmiş bulgu seçimi: uygun değil")}</summary><ul>{(record.history_eligibility?.reasons ?? [t("Eligibility has not been assessed yet", "Uygunluk henüz değerlendirilmedi")]).map((reason, index) => <li key={`${reason}-${index}`}>{reasonText(reason)}</li>)}</ul></details>}
        <details className="experience-eligibility"><summary>{t("Training preparation: review required · not ready for training", "Eğitim hazırlığı: inceleme gerekiyor · eğitime hazır değil")}</summary><ul>{record.training_eligibility.reasons.map((reason, index) => <li key={`${reason}-${index}`}>{reasonText(reason)}</li>)}</ul></details>
        <details><summary>{t("Record references", "Kayıt referansları")}</summary><dl><dt>{t("Record", "Kayıt")}</dt><dd>{record.record_id}</dd>{record.experiment_sha256 && <><dt>{t("Experiment SHA-256", "Deney SHA-256")}</dt><dd>{record.experiment_sha256}</dd></>}{record.trajectory_sha256 && <><dt>{t("Trajectory SHA-256", "Trajectory SHA-256")}</dt><dd>{record.trajectory_sha256}</dd></>}<dt>{t("Sources", "Kaynak")}</dt><dd>{record.provenance_summary.source_count} · {record.provenance_summary.usage_profile === 'noncommercial_research' ? t("Noncommercial research", "Ticari olmayan araştırma") : record.provenance_summary.usage_profile}</dd></dl></details>
      </article>)}</div>
      {current.records.length > 20 && !showAll && <button className="button secondary small" onClick={() => setShowAll(true)}>{t("Show all ", "Tüm ")}{current.records.length} {t(" records", " kaydı göster")}</button>}
      <p className="agent-note">{t("The downloaded preparation manifest contains verified references and rejection reasons only. No messages/data values, training or holdout are included. It is not an SFT package until eligibility has been reviewed.", "İndirilen hazırlık manifesti yalnız doğrulanmış referansları ve ret gerekçelerini içerir. Mesajlar/veri değerleri yok; eğitim ve holdout dahil değil. Uygunluk incelemesi yapılmadan SFT paketi değildir.")}</p>
    </>}
  </section>;
}
