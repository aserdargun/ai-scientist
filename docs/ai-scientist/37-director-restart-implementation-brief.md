# Director restart: Luna uygulama brief'i

Kaynak tabanı: `59ac3b059f0013ad2fb947cbc8820d891e244257` (0.31).
GPT-6 Astra / high kaynak incelemesi; çalışma veya kabul kanıtı değildir.
Bağlayıcı kapsam: spec §3.3.1–3.3.2 ve [35 numaralı kapsam incelemesi](35-director-restart-scope-review.md).

## 1. İlk teslimin sınırı

İlk tutarlı teslim **sahip nesli ve bütün yazma yollarının fencing'i** olsun:
ilk claim'i kalıcı nesle bağla, ham `lab run` girişini ortak yürütücüye taşı,
eski/eksik neslin yazmasını DB işlemi içinde reddet. Yalnız yeni bir resume
komutu eklemek bu teslimi karşılamaz. Deadline, retry ve ölçüm kökeni işleri
tamamlanmadan otomatik araştırma continuation'ı açma veya destekleniyor diye raporlama.

Çalışma ağacı: `data/runtime/parallel-m0/director-restart-032`, branch
`feat/luna-m0-director-restart-032`. Paylaşılan `.venv` ile kontrollerde bu
ağacın açık `PYTHONPATH` değerini kullan ve import kaynağını doğrula.
Baseline worker kendi `cli.py`, API contracts/service ve schema hunk'larına
sahip. Önce yeni ownership modülü, migration ve testleri hazırla; ortak
dosya adaptörlerini root ile entegrasyon sırasına bağla. Diğer worker'ın
dosyalarını değiştirme. `0025_director_generations` özel ağaçta `0021` üzerine
hazırlanabilir; root, `0022 → 0023 → 0024 → 0025` zincirini kurar.

## 2. Kalıcı sahiplik şeması

Mevcut `lab.director_run_owners` ilk sahip kaydı olarak değişmeden kalsın.
`lab.director_recoveries` içindeki `stop_and_finalize` sözleşmesini continuation
anlamına çevirmeden koru. Yeni tablolar:

| Tablo | Asgari alan ve kısıt |
|---|---|
| `lab.director_execution_contracts` | PK `run_id`; immutable `payload_sha256`, canonical `execution_json` ve hash'i; ilk `started_at`, `deadline_at`. Execution JSON suite/registry/provider-config/scenario/harness/image pinlerini içerir. Deadline ilk DB claim zamanından immutable request wall bütçesiyle hesaplanır. |
| `lab.director_owner_generations` | PK `(run_id,generation)`; `generation > 0`; aynı execution hash'i; PID/start ticks/boot/unit/InvocationID/cgroup; `claimed_at`; nullable `restart_id` (yalnız nesil 1'de boş). Append-only; run başına aynı invocation tekrar yeni nesil üretemez. |
| `lab.director_execution_control` | PK `run_id`; `(run_id,current_generation)` FK; `mode` = `active` veya `reconciling`; nullable `restart_id`; `updated_at`. Yalnız dar claim/reconcile RPC'leri değiştirir. Bu alan public run durumuna yeni `paused` değeri eklemez. |
| `lab.director_restart_requests` | PK `restart_id`; run + expected generation + request hash için unique; immutable amaç/payload/execution hash'leri; `pending/drained/claimed/rejected` kontrol durumu; observation receipt/blob hash'i; claimant generation ve result hash'i. Terminal sonuç immutable. |

Yeni queued claim aynı işlemde running geçişi, execution contract, ilk eski
owner kaydı, generation 1 ve current pointer'ı yazar. Kısmi sahiplik olmaz.
Tarihsel terminal koşular okunabilir kalır. Mevcut çalışan koşunun nesil 1
adaptasyonu yalnız gerçek immutable owner kaydından yapılabilir; çalışma
pinleri/checkpoint kökeni eksikse continuation reddedilir. Yeni deadline
üretme. Geçerli eski başlangıç kanıtı yoksa tahmin ederek resume etme.
Thompson v1 checkpoint'e sonradan RNG geçmişi icat etme.

## 3. İşlem içindeki fence

Yeni `lab/director/ownership.py`: immutable `ExecutionOwner` değeri
`run_id`, `generation`, `invocation_id`, `execution_sha256` taşır. Bu değer
ilk claim sonucu yakalanır; her yazımda DB'den son nesil okunup benimsenmez.
`DirectorRunLease` bu değeri zorunlu alır; advisory lock/heartbeat ek korumadır.

Dar SQL assertion/RPC, **yazımı yapan aynı transaction connection** üzerinde
expected generation ve invocation'ı current pointer ile `IS DISTINCT FROM`
kullanarak karşılaştırır; NULL/missing context reddedilir. Çalışma için
`running`, `stop_requested=false`, `mode=active` ve deadline şarttır.
Stop/reconcile/finalize için ayrı, dar yetkili geçişler tanımla; genel bir
`skip_fence` veya kullanıcı ayarlı bypass ekleme. Director rolü eski
imzaları/ham INSERT'leri kullanarak fence'i atlayamamalı: ilgili SQL
fonksiyonları ve tablo guard'ları da aynı sözleşmeyi uygular.

Kilit sırası mevcut plan kilidiyle uyumlu olmalı: run-plan transaction advisory
lock → run row → execution-control row → alt experiment/job/reservation rows.
Claim/takeover aynı sırayı kullanır. Global dispatch slotu lifecycle dışında
alınır. Özellikle job UPDATE önce row kilidi alıp trigger içinde run kilidi
istemesin; SQL girişleri ön kilitleri alsın. Kontrol ve mutasyon arasında ayrı
connection/commit kullanma.

### Zorunlu mutator envanteri

| Kaynak / giriş | Yapılacak bağlama |
|---|---|
| `cli.py`: `_dispatch_director_run`, `_record_claimed_dispatch_failure` | İlk claim ve geç failure/stop yazısı expected owner ile CAS. Yeni nesli eski exception handler terminalize edemez. |
| `journal.py`: `append_checkpoint` | Run lock ile aynı transaction'da nesil assertion; mevcut key/payload/sequence idempotency korunur. |
| `ledger.py`: `register_experiment`, `transition_experiment`, `commit_experiment_record` | Üç SQL RPC ve alt tablo guard'ları current owner gerektirir; immutable belge hash'leri değişmez. |
| `task_plan.py`: `plan_run_tasks`, `seal_run_task_plan`, `cancel_queued_score_job`, `record_planner_terminal_outcome` | Planner connection'ında fence; stop/recovery seal/cancel için dar ayrı yetki. |
| `scorer/jobs.py`: `enqueue_score_job`, `claim_score_job` | İşe admitting generation/execution identity kaydı. Eski Director yeni iş kabul ettiremez; eski worker yeni nesle ait işi claim edemez. |
| `scorer/service.py`, invocation/terminal trigger'ları | Geç score/terminal/final-report publication kendi admitted generation + worker claim'ine bağlanır. Yeni generation'a geçiş eski publication'ı reddeder. Önceden commit edilmiş doğru skorlar okunabilir kalır. |
| `baseline_runner.py`, `runner.py`, `loop.py` | Ledger/plan/journal çağrılarına immutable owner geçir; baseline calibration receipt, öneri ve bütçe checkpoint'lerini envantere dahil et. |
| `director/holdout.py`, migration 0019–0021 RPC'leri, holdout worker/recovery | Yeni admission/run-end intent/fence ve geç publication aynı nesli doğrular. Mevcut holdout worker invocation fence'i korunur; özel recovery RPC'si expected old generation + restart/stop intent doğrular. |
| `recovery.py`: intent/result/event ve stop/finalize | Orijinal ilk owner yerine current generation'ı gözlemle; eski recovery sonucu yeni generation'a uygulanamaz. |

API'nin yetkili kullanıcı stop isteği owner generation'a bağımlı değildir;
aynı run lock altında stop flag'i yazar ve devam claim'ini engeller. API
principal/idempotency alanlarını ve legacy request hash serialization'ını
değiştirme. Bağımsız baseline purpose'unu research continuation'a çevirmeme
kontrolünü, baseline entegrasyonundan sonra ekle.

## 4. Ortak giriş ve takeover sırası

`_run_director` yalnız doğrulanmış `ExecutionOwner` ile çağrılan iç yürütücü
olsun. Ham `lab run`, `_dispatch_director_run_owned` / global slot / registry
doğrulama yoluna yönlensin; yalnız DB'de `running` görmesi yeterli olmasın.
Yeni ve devam eden yürütme aynı immutable request'ten argüman üretir.
Provider construction ve bütün dış iş kabulleri sahiplik doğrulamasından sonra gelir.

Continuation sonraki teslimde: global slot + run lease al; current owner'ın
PID/start/boot/unit/InvocationID/cgroup kimliğiyle ölü olduğunu doğrula.
Run kilidi altında expected generation hâlâ aynıysa restart intent oluşturup
control'ü `reconciling` yap; bu sırada eski generation yeni iş yazamaz.
Sahipli Planner/Scorer/holdout/sandbox/model-call çocuklarını doğrula ve
uzlaştır. Belirsiz gözlem pending kalır. Yalnız exact drain tamamlandığında
tekrar aynı kilitlerle stop/terminal/deadline/pin kontrollerini yapıp N+1
history ve pointer'ı atomik yaz. İkinci contender yeni nesil oluşturamaz.
İlk ölülük gözlemi ile DB geçişi arasındaki yarışları receipt/generation
kontrolleriyle kapat; eski gözlem blob'unu tek başına yetki sayma.

## 5. Continuation açılmadan tamamlanacak sınırlar

**Bütçe:** `RunBudget.restore` mevcut reservation kimliklerini korur fakat
monotonic saati yeniden başlatır (`budget.py:237`). İlk DB deadline'ını
constructor/restore ve bütün admission noktalarına geçir. Etkin kalan süre,
DB deadline kalanı ile mevcut charged/reserved wall bakiyesinin minimumudur;
kesinti zamanı yeni bütçe yaratmaz. Token/proposal sayacı, pending provider
reservation ve Thompson seçimleri aynen kalır. Bütçe checkpoint seçimi durable
sequence sırasındadır; eski snapshot yeni tüketimi ezemez.

**Tek altyapı tekrarı:** `runner.py:793` içindeki görev başına bellekteki retry
döngüsü restart hakkı değildir. Kalıcı experiment-attempt kaydı kullan:
`(run_id,experiment_id,attempt_no)` unique, `attempt_no in (0,1)`; admitted
generation, başlangıç/deadline, budget reservation ID, typed infra sonucu ve
retry claim hash'i. Aynı deneyin farklı task/seed'i ikinci bir retry hakkı
üretemez. Bütçe + 120 saniye kuralı stale deneyi uzlaştırma eşiğidir; yeni
run bütçesi veya fazladan çalışma izni değildir. Exact child drain sonrası
tek retry hakkını **dış işi başlatmadan önce** atomik tüket. Retry sırasında
çökme hakkı sıfırlamaz. Aday çökme sayacı, posterior ve öneri ordinal'i infra
olayı için ilerlemez. Immutable terminal experiment belgesini yeniden açma;
altyapı attempt olayını nihai araştırma verdict'inden ayrı kaydet.

**Scorer öncesi köken:** `executor.py:105` sonrası enqueue'dan önce canonical
evaluation-evidence artifact + fenced checkpoint yaz: run/experiment/task/
seed/evaluation-kind, candidate/profile/harness/image/output hash'leri,
FitContext hash'i, guard sonuçları, fit/score süreleri, attempt/reservation
kimlikleri. Job bu evidence hash'ine bağlansın. Restart, receipt hash'i ve
Scorer'ın committed sonucunu birlikte doğrulayarak ölçümü yeniden kurabilsin.
`runner.py:_read_measurement` çıplak skor satırını kabul etmeye başlamasın.
Eksik kökeni sonradan uydurma; aynı score işi veya immutable terminal belgeyi
yeniden üretmeden kullan. Farklı output hash'i aynı job key'e yazılamaz.

## 6. Teslim ve açık kabul

İlk dilim için CPU sözleşme testleri ve izole SQL kanıtı: iki owner contender,
NULL/yanlış/eski nesil, kayıp eski DB bağlantısından geç yazı, raw CLI bypass,
stop/takeover yarışı, history immutability ve doğru kilit sırası. Baseline
worker hunk'ları birleşmeden ortak CLI/schema adaptörünü yayınlama.

Tam restart kabulü ayrıca gerçek süreç kaybını baseline içinde, Scorer commit'i
sonrasında ve terminal belge commit'i sonrasında ölçer: aynı run devam eder,
skor/belge/iş çoğalmaz; kesinti deadline'ı aşarsa yeni iş başlamaz; retry
ortasında çökme ikinci hak yaratmaz; bozuk pin/köken reddedilir. Full kalite
kapısı ve gerçek exit code, version/image parity, GPU/AOS birlikte çalışma,
gerçek veri/model ve kalan M0 maddeleri açık kalır. Fixture başarısını gerçek
restart veya tam M0 kabulü olarak raporlama.
