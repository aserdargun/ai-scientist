# Baseline ve ownership: tek izole entegrasyon kanıtı planı

2026-09-27 — GPT-6 Astra / high kaynak inceleme planı.
**Birleşik runtime kanıtı çalıştırılmadı; kabul veya entegrasyon onayı değildir.** Baseline ve 0025
kaynakları hâlen değişiyor. Bu plan gerçek public veri, model/GPU, AOS birlikte
çalışma veya tam Director restart kabulünü kapatmaz.

## Entegrasyon sırası

1. Baseline stop/rapor blokörlerini ve ownership mutator envanterini kaynak
   incelemesinde kapat; değişen dosyaların son hash'lerini dondur.
2. Root, ortak CLI/schema/API değişikliklerini birleştirip migration zincirini
   `0022 → 0023 → 0024 → 0025` yapar. Legacy request hash'leri, research varsayılanı
   ve tarihsel rapor okunabilirliği korunur.
3. Birleşik kaynakta odaklı testler, zorunlu kalite kapısı ve sandbox image/runtime
   parity tamamlanır. Harness fingerprint Director/API/Scorer kaynaklarını da
   içerdiğinden eski image veya yalnız 0024 snapshot'ı yeterli değildir.
4. Aşağıdaki senaryolar **tek disposable PostgreSQL kurulumu**, farklı kabul
   edilmiş run ID'leri ve sıralı yürütme ile çalıştırılır. Tam başarılı baseline
   matrisi bir kez ölçülür; stop ve fencing için küçük ayrı koşular kullanılır.

0025 gecikirse kaynak/CPU testleri ilerleyebilir. 0024-only yürütme kanıtı,
0025 fencing kanıtı diye sunulmaz. İlk 0025 teslimi yalnız ilk nesil sahipliği ve
yazma fence'lerini sağlıyorsa olmayan generation takeover/resume zorlanmaz;
bunlar açık kalır. İlerideki takeover testi tam baseline matrisini tekrarlamadan
küçük bir admitted run üzerinde yapılabilir.

## Tek driver'ın senaryoları

| Senaryo | Geçme koşulu |
|---|---|
| Gerçek admission | İzole API'de authentication açık; `POST /v1/baselines` gerçek service/registry yolundan geçer. Yetkisiz istek reddedilir. Aynı principal/key/payload aynı run'ı verir; değişen payload çakışması reddedilir. Immutable purpose, sıfır model bütçesi, task set, wall bütçesi ve request hash kaydedilir. Run oluşturmak için doğrudan SQL INSERT kullanılmaz. |
| Kullanılabilir CLI | Private loopback API'ye gerçek `lab baseline` komutu en az bir admission/status/report akışını tamamlar. Yalnız TestClient kullanılırsa kanıt açıkça ASGI integration olarak etiketlenir; gerçek HTTP CLI kabulü ayrıca açık kalır. |
| Dispatch ve sahiplik | Production dispatcher, global slot, run lease ve generation-1 claim birlikte kullanılır. İki eşzamanlı claim denemesi tek owner/history/current pointer ve tek yürütme üretir. Başka run'ın slotu sahiplenilemez. Raw CLI ortak sahiplik yolunu atlayamaz. |
| Tam baseline | Registry'ye fixture olarak kaydedilmiş dört ayrı sentetik aileden dört küçük EVT task üzerinde üç gerçek baseline algoritması × üç seed × tam dört task = **36 score**. Her görevin ağırlığı 0.25, family_cap 0.25, type_shares `{"EVT":1.0}` ve dört family_share 0.25 olur. Gerçek guard/fit/score sandbox ve bağımsız Scorer kullanılır; score satırı veya başarı receipt'i elle üretilmez. Eksik/fazla/çift hücre yoktur. |
| Bağımsız sonuç | Scorer'ın typed raporu, sealed plan, calibration receipt, experiment/trajectory çiftleri, canonical JSON hash ve API/report çıktısı tutarlıdır. Başarılı raporun calibration/budget doğrulamaları gerçekten tamamlanmıştır. Tekrar finalization belge/hash veya score sayısını değiştirmez. |
| Yasak yan yollar | Provider/model ve holdout launch sınırlarına çağrılırsa başarısız olan gözlem tripwire'ları konur; baseline hesaplaması değiştirilmez. Provider/model invocation, holdout reservation/promotion ve GPU claim yoktur. Model/token sıfırları doğrulanmış belge kimliklerinden gelir. |
| Queued stop | Gerçek authenticated stop, henüz dispatch edilmemiş baseline'ı boş planla typed incomplete sonuca götürür; deney veya model çağrısı yaratmaz. |
| Aktif worker stop | Gerçek bounded worker fazı ve kayıtlı PID/start/boot/unit/invocation/cgroup görüldükten sonra API stop gönderilir. Exact owned worker/container/marker drain dış gözlemle doğrulanır; ardından terminal sonuç ve bağımsız incomplete rapor oluşur. Başka run veya owner nesli etkilenmez. |
| Score/checkpoint aralığı | Gerçek Scorer score commit'i ve timing checkpoint'inin yokluğu, yalnız Director PID'si duraklatıldıktan sonra yeni DB okumasıyla birlikte doğrulanırsa API stop enjekte edilir. Aralık kaçırılırsa sonuç inconclusive'dır. Durable score korunur; eksik timing/guard veya tam seed ortalaması icat edilmez. Plan hücrelerinin her biri tam bir score veya terminal outcome taşır. Rapor `calibration_complete=false`, `budget_verified=false` ile bilinmeyeni dürüstçe gösterir. |
| Drain/finalization belirsizliği | Exact worker/marker kimliği doğrulanamazsa veya orijinal deadline tükenirse durum pending kalır; sahte stopped/başarılı rapor dönülmez. Finalizer process exit 0 tek başına başarı değildir: DB terminal state ve canonical report receipt okunur. |
| Roller | Gerçek Director/Planner/Scorer bağlantılarıyla gerekli dar RPC'ler çalışır; yetkisiz raw score, ledger ve outcome yazımı reddedilir. Planner'ın yasak score SELECT'i ve Scorer'ın geniş ledger SELECT'i ile gizli bağımlılık kurulmaz. Negatifler doğru rol/state üzerinde çalışır; SQLSTATE ve satırların değişmediği doğrulanır. |
| Owner fence | Eksik/NULL/yanlış generation, invocation veya execution hash ile checkpoint/plan/job/ledger/terminal/failure mutasyonları aynı transaction'da reddedilir. Stop ile claim yarışı yeni iş kabul ettirmez. Geç eski-owner exception handler'ı başka owner'ı terminalize edemez. Desteklenen geçiş varsa generation değişimi sonrası aynı negatifler tekrarlanır; yoksa takeover kabulü açık kalır. |

Her negatif doğru önkoşulda çalışmalıdır: terminal satırın başka nedenle
reddettiği bir çağrı owner-fence regresyonu sayılmaz. Başarılı kontrol çağrısı
ve before/after state/hash karşılaştırması aynı fixture'da bulunur.

Kaynak hazırlık düzeltmesi (2026-09-27): önceki iki-task/18-score önerisi
üretim manifest writer'ın 0.25 aile tavanına uymaz. Açık bir fixture muafiyeti
yoktur; loader'ın değişken cap alanını 0.5 yaparak şart gevşetilmez. Dört
bağımsız sentetik süreç/aile kimliği kullanılır; bu gerçek veri kaynağı çeşitliliği
veya public-data kabulü değildir. 36 hücrenin mevcut outer bütçeye sığması proof
öncesi kontrol edilir; yürütme sırasında deadline uzatılmaz.

0025 için iki özel regresyon zorunludur: (1) Geçerli owner A transaction context'i
altında doğrudan granted SQL RPC/row mutasyonu ile run B'ye yazma reddedilir;
yalnız GUC içindeki A'yı doğrulamak yeterli değildir. Gerçek target run ile
context bağlanır, A/B satırları değişmez ve kanonik lock sırası korunur.
(2) İlk claim'in wall parametresi ve execution JSON'u birlikte değiştirilse bile
immutable `request_json.budget.wall_seconds` ile uyuşmazlık SQL katmanında
reddedilir; owner/deadline/run state kısmen yazılmaz. Python validation bu
doğrudan SQL negatiflerinin yerine geçmez.

## Sentetik girdilerin ön doğrulaması — 2026-09-27

`baseline-proof-inputs-032.py`, üretim manifest writer ve registry sınıflarıyla
dört görevlik girdileri hazırladı. Session **19023 / exit 0**: iki aileli
manifest reddedildi; dört aileli manifest 0.25 aile tavanı ve dört eşit ağırlıkla
doğrulandı. Manifest 46.177 bayt; özel Scorer hazırlık dosyasında dört profil
ve 512 label satırı var. **36 skor planlandı, sıfır skor ölçüldü.** SQL kurulumu,
Planner DB readback, API/dispatcher ve sandbox/Scorer çalıştırılmadı.
Kaynak, dosya hash'leri ve gerçek çıkış kaydı:
[hazırlık](review-evidence/baseline-proof-input-preparation-e49b0286a157.json),
[execution binding](review-evidence/baseline-proof-input-preparation-binding.json).

Astra/high kaynak/dosya incelemesi 0700 klasör ve 0600 JSON modlarını doğruladı.
Hazırlık registry'si özel fixture klasörünü kök alır; production API ve CLI
`PROJECT_ROOT/data/runtime` kullanır. Gerçek proof'ta helper, snapshot içindeki
bu yeni runtime klasörü için **DSN'ler yazılmadan önce** çağrılmalı. Alternatif
olarak yollar o köke göre yeniden yazılıp registry/entry hash'leri yenilenmeli.
Mevcut hazırlık receipt'i doğrudan dispatcher readiness kanıtı değildir.

## Stop zamanlaması ve ölçümün sınırı

- Küçük sentetik veri, gerçek baseline implementasyonları ve üretim Scorer'ı
  kullanılır. Worker'ın aktif penceresi kaçırılırsa senaryo inconclusive'dır.
- Score/checkpoint aralığı için fixture run'ına filtreli PostgreSQL `AFTER INSERT`
  bildirimi kullanılabilir; `LISTEN` dispatch öncesinde commit edilir. Bildirim
  score transaction'ının commit edildiğini gösterir. Kimliği doğrulanmış Director
  ana PID'si pidfd ile duraklatıldıktan sonra score ve eksik timing checkpoint'i
  yeni DB okumasında birlikte görülmelidir. Bildirim tek başına aralığı kanıtlamaz.
  Aralık kaçırılırsa inconclusive kaydı korunur; otomatik tekrar yapılmaz.
  Scorer veya cgroup duraklatılmaz, DB kilidiyle terminal CAS engellenmez.
  API stop sonrasında ve hata/süre aşımında aynı pidfd üzerinden devam sinyali
  verilir. İlk deadline duraklama boyunca işler. Bu kontrollü kesinti kanıtıdır;
  doğal yarış, süreç restart'ı veya performans ölçümü değildir.
- Terminal DB job durumu tek başına process drain kanıtı değildir. Raw OS,
  systemd, Docker ve admission marker gözlemleri ayrıca tutulur. Observer
  terminal CAS'ı bloklayarak aranan sıralamayı kendisi üretmemelidir.
- Stop cleanup aynı immutable owner/payload altında kalır. Tek orijinal deadline
  ölçülür; recovery/finalizer çağrıları kalan süreyi aşamaz ve allowance yenilemez.
  Dispatcher'ın mevcut `requested_wall + 600s` unit üst sınırı araştırma bütçesi
  değildir; ayrıca kaydedilir.

### Gözlemci ön deneyi — 2026-09-27

`baseline-proof-owned-process-033.py` yalnız run'a bağlı Director kimliğini
PID/start/boot/unit/invocation/cgroup ile doğrular; tüm sinyaller aynı pidfd'ye
gider. Astra iki sınır hatasını düzelttirdi: duraklatılmış gözlemde `T` zorunluluğu
ve SIGSTOP syscall'ından önce devam cleanup'ının hazırlanması.

İlk küçük sentetik sleeper deneyi **session 62232 / exit 1** verdi: projenin
Python 3.12.13 derlemesinde iki pidfd Python API'si yok. Başarısız kayıt korunur;
40 saniyelik sleeper'ın doğal çıkışı ve boşalan exact cgroup ayrıca doğrulandı.
Capability ön kontrolü eklendikten sonra yalnız stdlib kullanan gözlemci sistem
Python 3.14.7 ile çalıştırıldı: **tool chunk eadab6 / exit 0**, dört kontrol geçti
(yanlış start kimliğinin sinyalsiz reddi, normal pause/resume, çağıran hatasında
resume, ilk süre aşıldığında resume). Sleeper ve cgroup temizlendi. Ölçülen
üst sınırlar observer için 1 GiB/0.5 CPU/64 task, sleeper için 64 MiB/0.1 CPU/8 task;
swap kapalıydı. Bunlar tepe kullanım ölçümleri değildir.

Bu deneyde DB, Director iş yükü, API, baseline score, Docker, GPU veya AOS yoktu.
Birleşik proof'ın Python 3.12 sürücüsü pidfd gözlemcisini ayrı stdlib süreçte
çalıştırmalıdır; ham sayısal PID sinyaline geri dönüş yapılmaz. Asıl checkpoint-gap
senaryosu açık. [Deney bağı](review-evidence/baseline-owned-process-probe-binding.json),
[SQL senaryo tasarımı](review-evidence/baseline-ownership-sql-proof-design-033.md).

## Yeniden kullanılacak kaynaklar

| Kaynak | Kullan / değişmeden çalıştırma sınırı |
|---|---|
| `review-evidence/review_care_calibration031_pg.py` | Disposable DB, private roles/DSN, kaynak snapshot/parity, immutable image ID ve exact cleanup iskeleti. Mevcut driver yalnız sabit SQL testine izin verir; worker proof için yeni adlandırılmış ve incelenmiş driver gerekir. 300s/45s bütçesi otomatik taşınmaz. |
| `review-evidence/holdout-030-lifecycle-fixture.py` | Snapshot/import isolation, owned child kimliği, cleanup reserve ve unrelated before/after gözlemleri. 0.30 DB/image/label ve holdout plugin varsayımları yeniden kullanılmaz. |
| `review-evidence/holdout-030-lifecycle-review.py` | Ham process/unit/container gözlem yaklaşımı. Eski run/version'a bağlı driver ve nedensel sıralama iddiası taşınmaz. |
| `review-evidence/review_baseline_pipeline.py` | Küçük sentetik task ve gerçek guard/baseline/Scorer hazırlığı. Eski migration0013, manual run INSERT ve varsayılan DB bağlantıları bu kanıt için uygun değildir. |
| `review-evidence/review_director_phase_stop.py` | Deterministik faz sınırı assertion'ları. Mevcut test gerçek in-flight worker drain veya authenticated admission kanıtı değildir. |
| `tests/director_dispatch_owner_review.py` | Owned dispatcher unit/global slot düzeni. Mock `_run_director` ve prematurity failure gerçek baseline çalışması yerine kullanılamaz. |

## İzolasyon ve kaynak sınırları

- Yeni UUID isim/label'li PostgreSQL, immutable yerel image ID, dinamik yalnız
  `127.0.0.1` port; private 0700 snapshot ve private dört rol/DSN. Ana DB/DSN veya
  user-manager global environment değiştirilmez. Registry ve fixture blob'ları
  yalnız bu snapshot'a aittir; her child'ın import kaynağı ve harness hash'i doğrulanır.
- PostgreSQL: 512 MiB, 0.5 CPU, 64 PID, 256 MiB tmpfs; outer driver/API: 1 GiB,
  0.5 CPU, 64 task. Swap kapalı. Mevcut Director: 2 GiB/1 CPU/128 task;
  aggregate Scorer slice: 2 GiB/1 CPU/32 task; sandbox: 4 GiB/2 CPU/128 PID.
  Bu bağımsız sibling sınırlarının muhafazakâr toplamı **9.5 GiB ve 5 CPU**'dur;
  outer cgroup'un bütün çocukları sınırladığı varsayılmaz. Gerçek cgroup limitleri
  ve peak'ler kaydedilir; shared slice limitleri değiştirilmez.
- Başlamadan en az 12 GiB MemAvailable ve 20 GiB boş disk ile mevcut AOS/diğer
  işlerin headroom'u doğrulanır. Bunlar önerilen admission tabanıdır; eşzamanlı
  host yükü için yeterli değilse başlatılmaz. GPU yok; CPU/P1 admission lock'u
  zorla alınmaz, yabancı süreç/marker silinmez.
- Önerilen toplam outer deadline en çok 1800s, bunun son 90s'si owned cleanup'a
  ayrılır. Başarılı run ve stop koşularının ayrı immutable wall bütçeleri bu
  toplama sığacak şekilde önceden sabitlenir. Driver içinde otomatik yeniden
  deneme veya deadline uzatma yapılmaz; her subprocess kalan süreyle sınırlıdır.
- Cleanup yalnız tam kimliği doğrulanmış fixture unit/container/marker/DSN'lerini
  kapsar. Başarısızlık kanıtı korunur; cleanup başarısızlığı nonzero sonuçtur.
  Önceden var olan servis/container/cache'ler için before/after kayıt tutulur.

## Teslim edilecek kanıt

Tek JSON execution binding ve okunabilir özet: birleşik commit/tree, tüm kaynak
ve fixture adapter hash'leri, migration head, sandbox image ID/harness parity,
gerçek session/exit code, outer/child limitleri ve peak'leri; token/DSN içermeyen
API status'ları; fixture run/owner/job kimlikleri; 36-cell matrix, rapor canonical
hash/readback, role-negatif SQLSTATE'leri; stop milestones ve raw drain gözlemleri;
owned cleanup ve unrelated before/after sonuçları. Başarısız veya inconclusive
senaryo silinmez, başarılı kardeş senaryolardan ayrılır.

Tam restart continuation, orijinal deadline'ın nesiller boyunca korunması,
kalıcı tek infra retry ve pre-Scorer provenance kabulü ancak uygulanmış yolları
gerçekten yürütülürse kapanır; bu ilk ownership/baseline kanıtıyla otomatik kapanmaz.
