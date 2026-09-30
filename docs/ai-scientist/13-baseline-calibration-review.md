# Referans algoritmalar ve kalibrasyon incelemesi

Tarih: 2026-09-24. Başlangıç commit'i `401b16f`. Bu kayıt [ECOD kaynak incelemesini](12-director-baseline-review.md) izler; gerçek Docker/Scorer kalibrasyonu ve Director kabulü ayrıca kanıt gerektirir.

## Uygulama sözleşmesi

Üç sürümlü algoritma `harness/baselines.py` içinde tanımlanır: `robust-z.v1`, `isolation-forest-sklearn.v1`, `ecod-train-frozen.v1`. `lab/director/baselines.py` bu izin listesinden deterministik aday kaynak baytları üretir ve dev sonuçlarını kalibrasyon belgesine dönüştürür. Algoritmalar normal Docker fit/score yoluna girmelidir; kaynak dosyasındaki yeni algoritmanın eski sandbox imajında bulunduğu varsayılamaz.

- Robust-z eğitim medyanını ve `1.4826 × MAD` ölçeğini kullanır. Sıfır MAD için sırasıyla IQR/1.349, standart sapma ve sabit sütunda 1.0 yedeği tanımlıdır. Satır skoru sensörler arasındaki en büyük mutlak z değeridir.
- Isolation Forest 100 ağaç, en çok 256 eğitim satırlı örnekleme, `contamination='auto'`, `ctx.seed`, `n_jobs=1` kullanır. Skor `-decision_function` ile yüksek değer daha aykırı olacak biçimdedir.
- ECOD uyarlamasında eğitim ECDF'leri ve çarpıklık yönü fit sırasında sabitlenir. Sol kuyruk `P(train ≤ x)`, sağ kuyruk `P(train ≥ x)`; alt sınır `1/(n+1)` olur. Özellik katkısı `max(U_skew, U_l, U_r)`; sıfır çarpıklıkta `U_skew=U_l+U_r` olur. Değerlendirme satırları eğitim dağılımına katılmaz. Bu uyarlama upstream transductive ECOD ile sonuç eşliği iddiası taşımaz.
- Alarm politikası yalnız eğitim skorlarının %99,5 niceliğinden çıkar. Eğitim skorları sabitse eşik ve release, sabit değerin bir sonraki sonlu float değerine eşitlenir; böyle bir değer yoksa açık hata verir. Böylece sabit normal skorlarda alarm başlamaz ve geçici bir yükselmeden sonra kapanabilir.

Görev normalizasyonu özgün `clip((raw-base)/max(ref-base, δ_tip), -1, 3)` formülünü ve tip tabanlarını korur. Referans özeti `mean-seeds-0-2.v1`: her algoritmanın tam 0, 1, 2 tohumlarının aritmetik ortalaması; `base` robust-z ortalaması, `ref` üç ortalamanın en büyüğü. Aday geldikçe bu değerler yeniden hesaplanmaz. Gürültü özeti `population-ddof0-seeds-0-2.v1`, şampiyonun üç ayrı **süit skorunun** popülasyon standart sapmasıdır; görevler arası skor yayılımı değildir.

### Kalibrasyonun saklanması

Uygulama kararı: kalibrasyon tek canonical blob ve run'a bağlı değişmez digest/kimlik kaydıyla saklanır. İlk aday önerisine bağımlı olmamalıdır; böylece aday üretilmeden önce yeniden başlatılan koşu da aynı kalibrasyona ulaşabilir. Snapshot run/suite/harness/image kimliklerini, tam görev kümesini, algoritma ve kaynak sürümlerini, ölçülen üç tohumun skorlarını ve Scorer artifact referanslarını, base/ref/taban/nihai ağırlıkları ve gürültü hesabı girdilerini taşır. Aday bağlamı ve replay aynı digest'i kullanır. Aynı kaydın tekrarı idempotent, farklı içerikle değiştirme reddedilmiş olmalıdır. Kaynak ölçümler yalnız dev sonuç view'ından alınır; LLM'in verdiği skorlar kalibrasyon değildir. Bu kalıcı yolun sentetik EVT üzerindeki gerçek yazma/okuma kanıtı aşağıdadır; diğer görev tipleri ve tam Director bağlantısı açık kalır.

Sabit skor istisnası yalnız harness'in sabit referans algoritmaları içindir: supervisor `trusted_baseline_name` ile tam izinli kaynak baytlarını eşleştirir; bu özel modda referansın sabit çıktısı ölçülebilir. Araştırma önerileri normal constant-score reddine tabidir. Adayın kendi ortam değişkenini değiştirmesi host doğrulamasını devre dışı bırakmamalıdır. Bu sınırın yeni imajdaki dört gerçek kontrolü aşağıda kayıtlıdır.

## Taslakta doğrulanan iki hata

[Sayısal karşı örnek kaydı](review-evidence/baseline-draft-numerics-before.json), taslak kaynak SHA-256 `5a87360ab1b5195ae6484305866da649b1c67890fe1816d1e2cdb683ad64b226` üzerinde gerçek yerel CPU hesaplarını tutar:

1. İlk ECOD taslağında son `max(U_skew,U_l,U_r)` eksikti. Eğitim `[0,0,0,1,10]`, değerlendirme `-10` için 0 üretildi; tanımlı uyarlamada beklenen `ln(6)=1.791759469228055`. İşaretleri ters çevrilmiş örnekte de aynı hata görüldü.
2. Tamamen sıfır eğitim skorlarında threshold `5e-324`, release negatif epsilon olunca `[0,1,0,0]` skorları `[false,true,true,true]` alarmı üretti. Normal değere dönünce alarm kapanmıyordu.

İki hata Luna tarafından düzeltildi. Negatif kayıt başarı veya kabul kanıtı değildir; hangi gözlemin düzeltmeyi gerektirdiğini gösterir.

## Bağımsız sayısal doğrulama

`OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 .venv/bin/python docs/ai-scientist/review-evidence/review_baseline_numerics.py` → **35/35, exit 0**, `source_unchanged=true`. [Sonuç ve kaynak hash'leri](review-evidence/baseline-numerics-review.json).

ECDF oracle'ı üretimdeki sıralı aramayı kullanmaz; sabit örneklerde her eğitim değerini doğrudan sayar ve önceden bilinen çarpıklık yönünü kullanır. Pozitif/negatif/sıfır çarpıklık, eşit değerler, sabit özellik, eğitim dışı kuyruklar ve iki özellik toplamı doğrulandı. Üç algoritmada geleceğe eklenen satırlar mevcut prefix skorunu değiştirmedi; aynı tohumla yeniden fit aynı skorları verdi. Sabit eğitim alarmının normale dönüşü, bağımsız medyan/MAD hesabı ve beş tipin normalizasyon tabanları/kırpması da geçti.

**Kanıt sınırı:** Bu kontroller güvenilir baseline Python kodunun yerel CPU aritmetiğini sınar. Sandbox imajının aynı kodu çalıştırdığını, bağımsız Scorer/PostgreSQL ölçümlerini, fiziksel blobları, çok görevli kalibrasyonu, 20 deneylik Director'ı, gerçek kamu verisini, yerel LLM'i veya AOS birlikte çalışmasını kanıtlamaz. Bu yüzden M0.8/M0.9 bu kayıtla kapanmaz.

## İmaj hazırlığı

Kaynakların sabit bir kopyasından `docker build --network=none` ile hazırlık imajı oluşturuldu: `sha256:f00b2ee35357ad629fbb3cccabcd497c03f2df1d4c3738f5b5d47dbd14035feb`. Derleme ve sınırlı import kontrolü exit 0 ile bitti. Ağsız, GPU'suz, non-root, salt okunur, 1 CPU/512 MiB konteynerde üç baseline sınıfı yüklendi; 57 runtime kaynak dosyasının baytları derleme kopyasıyla eşleşti. [Kaynak ve import kanıtı](review-evidence/baseline-image-import-review.json).

Bu yalnız hazırlık kontrolüdür: fit/score çalıştırılmadı, `ops/sandbox-image.lock` bu aşamada değiştirilmedi. Director dosyaları geliştirme sırasında değiştiğinden bu imaj güncel tüm çalışma ağacının eşliği olarak sunulmaz. Gerçek kalibrasyon ölçümünden önce son kaynak kopyasıyla imaj yeniden kurulup sabitlenecektir.

Son kaynaklar sabitlendikten sonra imaj yeniden kuruldu: `sha256:7b4a99a985704ab66ff8dd39cf13db257690d20eb8f67a38d58586807be988fc`. `ops/sandbox-image.lock` bu imaja geçirildi. Ağsız, non-root, salt okunur, 1 CPU/512 MiB kontrol konteynerinde 59 runtime kaynak dosyasının hash'leri hem build kopyasıyla hem mevcut host dosyalarıyla eşleşti; build ve kontrol exit 0. [Son imaj kanıtı](review-evidence/baseline-final-image-review.json).

## Kalibrasyon veritabanı geçişi

`0013_baseline_calibration` yalnız Lab veritabanında uygulandı. Ön kontrol: 0012 revision, sıfır koşu ve sıfır Scorer işi. Geçiş exit 0, kaynak SHA değişmedi; [uygulama kaydı](review-evidence/baseline-migration-apply.json). Director-only receipt/register işlevleri, aynı içerikle kapalı koşuda da idempotent tekrar, tek canonical blob hash'i ve sonraki proposal belgelerinde bu hash'e bağlanma eklendi. [Rol grant kontrolü](review-evidence/baseline-receipt-role-check.json) Director için yalnız işlev çağrısını, Planner/Scorer için erişim yokluğunu doğrular; bu kontrol tek başına işlev davranışı testi değildir.

Bu dilimin kalibrasyonu yalnız EVT/VUS-PR kabul eder. PDM/NRM ve mod görevlerini bu metrikle yanlış etiketlememek için diğer görev tipleri reddedilir; onların bağımsız Scorer bağlantısı açık kapsamdır. İlk şampiyon robust-z'dir; görev referansı üç baseline ortalamasının en iyisi olarak ayrı saklanır. Şampiyon gürültüsü, ölçülmüş robust-z sonuçlarının normalize edilip görev ağırlıklarıyla toplanan üç süit skorundan çıkar. Girdi görevleri hesaplamadan önce canonical sıraya alınır.

## Gerçek Docker / Scorer kalibrasyonu

`OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 .venv/bin/python docs/ai-scientist/review-evidence/review_baseline_pipeline.py` → **36/36 ölçüm, 7/7 son kontrol, exit 0**. [Kaynak kimlikleri, ölçümler, belge hash'leri ve rapor](review-evidence/baseline-pipeline-review.json).

- Dört yerel sentetik EVT profili, her birinde 64 train / 64 eval satırı ve iki sensör. Etiketler yalnız Scorer veritabanına yazıldı. Bu fixture'lar kamu veri kabulü değildir.
- Üç algoritma çalışmadan önce Director rolüyle kaydedilip `primary_running` durumuna geçirildi. Planner toplam 36 algorithm/task/seed atamasını yaptı. Her ölçüm ayrı Docker fit/score, tam fit determinism ve frozen-fit causality kontrollerinden geçti. Her skor 2 GiB/1 CPU sınırındaki ayrı Scorer systemd sürecinde üretildi; 36 farklı worker PID kaydedildi.
- Candidate, inputs, messages, experiment ve trajectory baytları generic blob deposuna gerçekten yazılıp hash doğrulamasıyla geri okundu. Candidate ağaç hash'leri ayrı fixture Git deposunda oluşturulmuş gerçek ağaç objeleridir. Baseline belgelerinde `predicted_delta`, `decision` ve trajectory `outcome` null; uydurma Referee kararı yoktur. Belge içindeki görev VUS değerleri üç tohumun ortalaması, süreler bu üç ölçümün toplamıdır; tek tek tohum sonuçları kalibrasyonda saklanır.
- Tam grid yalnız `lab.dev_task_results` üzerinden okundu. Canonical kalibrasyon SHA ve fiziksel blob SHA eşittir: `c219a43540699eb7ffc41fa539ef32f458444415a62aee78a1fc44094f2a29cf`. İstek görevleri ters sırada verilmesine rağmen canonical belge üretildi. Başlangıç robust-z şampiyonunun üç normalize süit skoru `[0.0,0.0,0.0]`, noise `0.0` oldu.
- Task planı 36 sonuçla kapatıldı; ayrı Scorer finalizer 36 skorlu raporu üretti. Rapor SHA geri hesaplanarak doğrulandı. Terminal koşuda aynı kalibrasyonun tekrarı `already_registered` döndürdü. Yalnız bu testin run/profile veritabanı satırları temizlendi; fiziksel review dosyaları kanıtta belirtilen özel runtime dizininde tutulur.

Ölçüm boyunca harness SHA `01d2f06369cc4ed63b5611d066be1acc82046d9c960fc5419b04ee70730f77c4` ve yukarıdaki son imaj sabit kaldı. İlk test denemesinde sürücü `primary_running` geçişini atladığı için veritabanı `proposed → scored` işlemini reddetti. [Başarısız sürücü denemesi](review-evidence/baseline-pipeline-driver-before.json) korunur; üretim kodunu gevşetmeden sürücü düzeltildi ve başarılı koşu yeni kimlikle baştan çalıştırıldı.

## Sabit skor istisnasının sınırı

`OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 .venv/bin/python docs/ai-scientist/review-evidence/review_baseline_boundary.py` → **4/4, exit 0**, aynı harness/image kimliği ve değişmeyen kaynak. [Kontroller](review-evidence/baseline-boundary-review.json): normal sabit aday reddedildi; `SWAPP_TRUSTED_BASELINE=1` ortamını adayın kendisinin yazması host tarafındaki constant guard'ı aşamadı; izinli baseline kaynağının sonuna yorum eklenmesi bile exact-source kontrolünde reddedildi; yalnız tam robust-z kaynağının sabit referans çıktısı kabul edildi.

**Açık kapsam:** Genel Director/CLI ile 20 aday, Referee teyidi ve replay; PDM/NRM/mod görevi kalibrasyonu; gerçek public split/suite; gerçek yerel model/eğitim ve AOS birlikte çalışma. Yukarıdaki kanıtlar M0.8/M0.9'u tek başına kapatmaz ve tarihsel 19 guard vakasının tamamını yeni sürüme taşımış sayılmaz.

Ortak blob yazım kodu genişletildiği için mevcut gerçek çökme kontrolü güncel kaynakla yenilendi: `.venv/bin/python docs/ai-scientist/review-evidence/review_blob_crash_recovery.py` → **10/10, exit 0**. Yalnız bu testin özel dizinindeki yeni writer, dosya fsync'inden sonra SIGKILL ile durduruldu; sonraki yazım orphan geçici dosyayı kaldırdı, önceki tamamlanmış blob ve yanındaki sentinel korundu, hash/tekrar kontrolü geçti. [Güncel kanıt](review-evidence/blob-writer-crash-recovery-review.json) numeric score wrapper üzerinden ortak generic byte writer'ı sınar; tam Director restart veya kalibrasyon DB/blob transaction crash kabulü değildir.
