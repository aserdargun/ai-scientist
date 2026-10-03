import { t } from './i18n';
type NumericField = { key: string; label: [string, string]; value: number; min: number; max: number; step?: number };
const groups: { name: [string, string]; method?: string; fields: NumericField[] }[] = [
  { name: ['Mode tolerance and alarm', 'Mod toleransı ve alarm'], fields: [
    { key: 'min_support', label: ['Minimum mode support', 'Asgari mod desteği'], value: 4, min: 2, max: 512 },
    { key: 'tolerance_quantile', label: ['Tolerance quantile (0–1)', 'Tolerans yüzdeliği (0–1)'], value: .99, min: .001, max: 1, step: .001 },
    { key: 'tolerance_multiplier', label: ['Tolerance multiplier', 'Tolerans çarpanı'], value: 1.25, min: .001, max: 20, step: .001 },
    { key: 'alarm_quantile', label: ['Alarm quantile (0–1)', 'Alarm yüzdeliği (0–1)'], value: .99, min: .001, max: 1, step: .001 },
    { key: 'release_ratio', label: ['Alarm release ratio', 'Alarm bırakma oranı'], value: .8, min: 0, max: 1, step: .01 },
    { key: 'dwell', label: ['Consecutive samples for alarm', 'Alarm için ardışık örnek'], value: 1, min: 1, max: 1000 },
    { key: 'calibration_fraction', label: ['Chronological calibration fraction', 'Kronolojik kalibrasyon payı'], value: .2, min: .001, max: .499, step: .001 },
  ] },
  { name: ['LSH', 'LSH'], method: 'lsh', fields: [
    { key: 'lsh_tables', label: ['LSH table count', 'LSH tablo sayısı'], value: 4, min: 1, max: 8 },
    { key: 'lsh_projections', label: ['LSH projection count', 'LSH projeksiyon sayısı'], value: 3, min: 1, max: 8 },
    { key: 'lsh_width', label: ['LSH bucket width', 'LSH kova genişliği'], value: .35, min: .001, max: 10, step: .001 },
    { key: 'lsh_merge_tables', label: ['LSH table merge threshold', 'LSH birleştirme tablo eşiği'], value: 2, min: 1, max: 8 },
  ] },
  { name: ['OPTICS', 'OPTICS'], method: 'optics', fields: [
    { key: 'optics_min_samples', label: ['OPTICS minimum samples', 'OPTICS asgari örnek'], value: 8, min: 2, max: 512 },
    { key: 'optics_max_eps', label: ['OPTICS maximum epsilon', 'OPTICS azami epsilon'], value: 2, min: .001, max: 100, step: .001 },
    { key: 'optics_xi', label: ['OPTICS xi', 'OPTICS xi'], value: .05, min: .001, max: .999, step: .001 },
    { key: 'optics_min_cluster_size', label: ['OPTICS minimum cluster size', 'OPTICS asgari küme boyutu'], value: 8, min: 2, max: 512 },
  ] },
  { name: ['SOM', 'SOM'], method: 'som', fields: [
    { key: 'som_rows', label: ['SOM map rows', 'SOM harita satırları'], value: 3, min: 1, max: 12 },
    { key: 'som_columns', label: ['SOM map columns', 'SOM harita sütunları'], value: 3, min: 1, max: 12 },
    { key: 'som_iterations', label: ['SOM iterations', 'SOM iterasyonları'], value: 400, min: 1, max: 10000 },
    { key: 'som_learning_rate', label: ['SOM learning rate', 'SOM öğrenme oranı'], value: .3, min: .001, max: 1, step: .001 },
    { key: 'som_sigma', label: ['SOM neighborhood sigma', 'SOM komşuluk sigma'], value: 1.5, min: .001, max: 20, step: .001 },
  ] },
];
export type Parameters = Record<string, number | string>;
export function scenarioName(value: string): string {
  const names: Record<string, [string, string]> = {
    healthy_single: ['Healthy single mode', 'Sağlıklı tek mod'],
    healthy_multiple: ['Healthy multiple modes', 'Sağlıklı çoklu mod'],
    healthy_load: ['Healthy load variation', 'Sağlıklı yük değişimi'],
    step: ['Step change', 'Basamak değişimi'],
    drift: ['Drift', 'Kayma'],
    variance: ['Variance change', 'Varyans değişimi'],
    correlation_break: ['Correlation break', 'Korelasyon kırılması'],
    oscillation_lag: ['Oscillation and lag', 'Salınım ve gecikme'],
    unseen_mode: ['Unseen mode', 'Görülmemiş mod'],
    sensor_quality: ['Sensor quality', 'Sensör kalitesi'],
  };
  const name = names[value];
  return name ? t(...name) : value;
}
export const defaultParameters: Parameters = {
  ...Object.fromEntries(groups.flatMap(group => group.fields.map(field => [field.key, field.value]))),
  weighting: 'distance', metric: 'euclidean', calibration: 'leave_one_out',
};
export function parameterError(values: Parameters): string | null {
  for (const group of groups) for (const field of group.fields) {
    const value = values[field.key];
    if (typeof value !== 'number' || !Number.isFinite(value) || value < field.min || value > field.max || (!field.step && !Number.isInteger(value))) return t(`${t(...field.label)}: enter a valid value between ${field.min}–${field.max}.`, `${t(...field.label)}: ${field.min}–${field.max} aralığında geçerli bir değer girin.`);
  }
  return Number(values.lsh_merge_tables) > Number(values.lsh_tables) ? t('The LSH merge threshold cannot exceed the table count.', 'LSH birleştirme eşiği tablo sayısını aşamaz.') : null;
}
export function ModeParameters({ methods, values, onChange }: { methods: string[]; values: Parameters; onChange: (values: Parameters) => void }) {
  return <details><summary>{t('Method hyperparameters, tolerance and alarm', 'Yöntem hiperparametreleri, tolerans ve alarm')}</summary>
    <p>{t('Mode tolerance and the alarm threshold are learned only from training data. SOM distance and OMR are separate measurements.', 'Mod toleransı ve alarm eşiği yalnız eğitim verisinden öğrenilir. SOM uzaklığı ve OMR ayrı ölçümlerdir.')}</p>
    <div className="run-form mode-parameters">
      <label>{t('Neighbor weighting', 'Komşu ağırlığı')}<select value={values.weighting} onChange={e => onChange({ ...values, weighting: e.target.value })}><option value="distance">{t('By distance', 'Uzaklığa göre')}</option><option value="uniform">{t('Uniform', 'Eşit')}</option></select></label>
      <label>{t('Distance metric', 'Uzaklık metriği')}<select value={values.metric} onChange={e => onChange({ ...values, metric: e.target.value })}><option value="euclidean">{t('Euclidean', 'Öklid')}</option><option value="manhattan">Manhattan</option></select></label>
      <label>{t('Calibration method', 'Kalibrasyon yöntemi')}<select value={values.calibration} onChange={e => onChange({ ...values, calibration: e.target.value })}><option value="leave_one_out">{t('Leave one out', 'Birini dışarıda bırak')}</option><option value="chronological">{t('Chronological', 'Kronolojik')}</option></select></label>
      {groups.filter(group => !group.method || methods.includes(group.method)).map(group => <fieldset key={group.name[0]}><legend>{t(...group.name)}</legend>{group.fields.map(field => <label key={field.key}>{t(...field.label)}<input type="number" min={field.min} max={field.max} step={field.step ?? 1} value={values[field.key]} onChange={e => onChange({ ...values, [field.key]: e.target.value === '' ? '' : Number(e.target.value) })}/></label>)}</fieldset>)}
    </div>
  </details>;
}
