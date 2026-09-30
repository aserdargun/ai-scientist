# M0 planı

Tarih: 2026-09-24. Uygulama bu planı onay beklemeden yürütür. Karar kapıları `m0-acceptance.md` içinde tutulur.

2026-09-27 ürün yönü: bağımsız açık kaynak AI/ML laboratuvarı; kullanıcı
SWAPP+AOS için özel fork geliştirecek, diğer kullanıcılar farklı sistemler
bağlayabilecek. Model/yöntem kataloğu, skill ve eğitim kaydı teslim sırası
[44 numaralı planda](44-open-laboratory-roadmap.md). Mevcut M0 ve OM
kabulleri korunur; README geliştirme sayaçları her teslimatta yenilenir.

## Kalan işlerden devam — 2026-09-30

Ana runtime **0.41.0**, güncel model tercihi **GPT-6.1 Sol / medium**.
M0 tablosunda **11 geçti / 7 kısmi / 4 açık**; OM.1–7 ve açık laboratuvar
planındaki ek işler ayrıca korunur. Bu sayı genel proje yüzdesi değildir.
1247 testlik zorunlu kapı yedi komut / gerçek exit 0 ile tekrar geçti.
Aserdargun SSH bağlantı scriptleri hazır; Windows gerçek çalıştırması açık.
243 public baseline ölçümü bağımsız doğrulandı; ilgili araştırma failed ve
proposal sayısı 0. Bu araştırma tamamlanmış sayılmaz.

Çalışma sırası; çalışan sistemi tamamlamak için:

1. **Normal stop akışının terminal kapanışı:** AOS yeniden bağlanma/yeni yetki,
   tekrarlı stop ve foreground gerçek süreçte geçti. Aynı iş ayrı normal
   recovery ile `stopped` ve hash-doğrulamalı rapora geçti. Şimdi UI'nin
   kullandığı normal queue drain ve doğrudan dispatcher için otomatik kapanış
   inceleniyor; mevcut owner/generation, deadline ve bütçe kapıları korunacak.
   Gerçek Scorer inflight stop ve eksik provider-budget restart sınırı açık.
   2026-09-30 yeni izole denemede canlı Scorer ve typed stop tekrarı görüldü;
   otomatik terminal rapor başarısız, ana servis geri yüklemesi geçti.
   API'nin önceden `stop_requested` yaptığı durum için hata kapanışı
   düzeltmesi ayrı adayda sürüyor. [Sonuç ve yayın notu](64-automatic-stop-and-publication.md).
2. **AOS uygulanabilir teslimi:** izole güncel kaynağa bağlı iki dar düzeltme,
   manifest ve package validation; normal opt-in bootstrap belgeleri. Canlı
   AOS'a uygulama paralel geliştirmeyle koordine edilecek. Önceki koşular,
   belirsiz intent'ler ve kaynak kopyaları korunacak.
3. **Gerçek yerel model / birlikte çalışma:** Qwen S1/S2 ile ≥6 öneri,
   her türden ≥2, parse hata oranı/kapasite/egress ölçümleri ve AOS'un
   etkileşimli işiyle adil GPU ilerlemesi. Gerçek opt-in GPU oturumu için
   mevcut koordinasyon yanıtı bekleniyor; başka süreçler kapatılmayacak.
4. **Gerçek public araştırma / holdout:** mevcut dört kaynak, gerçek başarılı
   aday ve run-end/10 KEEP holdout, bit-only/canary ve bağımsız replay.
   Başarısız eski run yeniden etiketlenmeyecek veya sıfırlanmayacak.
5. **İzolasyonun güncel kanıtları ve eğitim:** mevcut kabulde belirtilen
   sandbox recertification; izinli gerçek SERVE↔TRAIN/noop ve 24k/3 adım
   QLoRA kapasite ölçümü. Yasaklanan süreç bellek probe'u tekrar edilmeyecek.
6. **Uzun ML laboratuvarı kapsamı:** geniş sentetik havuzun Director/Scorer
   icrası, sürekli NN/residual/OMR akışı, uzun yerel agent deneyi ve bu akışın
   arayüzden durdur/devam/raporu (OM.1–7).
7. **Açık kaynak teslimi ve sonraki laboratuvar dilimleri:** taşınabilir
   kurulum/yeni makine testi, genel adaptörler, model/skill/eğitim kayıt
   zinciri. Lisans seçimi ve Git remote/auth henüz yok; PR/yayın açık.
   [44 numaralı yol haritasındaki](44-open-laboratory-roadmap.md) M0 sonrası
   işler tamamlanmış sayılmayacak.

Bağımsız CPU işleri paralel kaynak incelemeleriyle yürür; gerçek ağır
CPU/PG/gate işleri ortak admission lock ile sıralanır. Yeterli kanıt elde
edilen adım kapatılır; yeni bir risk veya zorunlu kapı yoksa tekrar test
edilmez. Her teslimatta README süre/token/model sayaçları güncellenir.

## Önceki durum — 2026-09-29

2026-09-29 durum kaydı: kalan işler **GPT-6.1 Sol / medium** ile yürütülür;
ana runtime **0.36.2**. Bağlı CPU arayüzü,
PostgreSQL sensör/dönem seçimi, ayrıntılı istatistik/JSON, sentetik kayıt,
LSH/OPTICS/SOM hiperparametreli deney başlatma ve terminal finalizasyonu
entegre. Son birleşik kalite kapısı 926 test ve yedi komutla exit 0;
278 gate dosyasının ana kaynaklarla eşliği doğrulandı. Önceki 36 ölçümlü
sentetik baseline ve 12 ölçümlü LSH/OPTICS/SOM projesi
tamamlandı. Mod raporunun API/DB/hash eşliği, OMR/sensör katkıları ve JSON
indirme dahil 19 tarayıcı kontrolü geçti. [Deneme rehberi](43-first-project-guide.md).
EXPLORE ve desteklenen resume kaynakları entegre. Gerçek süreç kesintisi
denemesi 053'te nesil devri ve dokuz mevcut skorun korunması doğrulandı;
tamamlanmış provider aşamasına kesinti süresinin eklenmesi nedeniyle devam
başarısız oldu. Süre kaydı düzeltmesi 19 odaklı kontrol ve 926 testlik
tam kalite kapısıyla dağıtıldı; gerçek 054 devam koşusu 20 öneri, 29 skor
ve 23 geçerli belge çiftiyle tamamlandı. Bağımsız 38/38 kontrol API/DB/rapor
eşliğini ve özgün son tarihi doğruladı. Kesinti anında provider bütçesi
zaten uzlaştırılmıştı; eksik uzlaştırma sınırının gerçek kabulü açık.
Eski stop kapanışında bulunan rol hatası mevcut dar RPC ile
düzeltildi; 35 gerçek PostgreSQL testi ve eski koşunun doğrulanmış `stopped`
raporu geçti. Arayüzde izlenen koşunun türü de düzeltildi. Dört gerçek kamu kaynağı
kuruldu ve 36 hücrelik baseline 671 saniyede tamamlandı (API/DB/hash ve dört
tarayıcı kontrolü geçti); 82 sensör/1.028 çift için
eğitim istatistikleri ayrıca ölçüldü. Gerçek model/public araştırma/AOS
kabulleri açık.
Ayrıntı [kabul tablosunda](m0-acceptance.md) ve
[ek kapsamda](42-operating-modes-omr-experiments.md).

## Bulgular ve ölçümler

- Kaynak mimari incelemesi: [03-architecture-review.md](03-architecture-review.md), düzeltme önceliği: [04-m0-review-addendum.md](04-m0-review-addendum.md). Ek C sentetik referans testleri geçmiştir; ek negatif örneklerde aynı nesneyle causal ihlal 1.0, maskeli PDM puanı 0.795918 ve sıfır sağlıklı maruziyette NaN metrikler görülmüştür. 100,000 null senaryo simülasyonu, korelasyonlu görev/ailelerde teyit edilmiş KEEP oranlarının nominal %2'yi aşabildiğini göstermiştir; Referee v1 yalnız dev şampiyonu seçer.
- Host ölçümü: CachyOS, Ryzen 5 7600 (6 fiziksel/12 mantıksal CPU), RTX 4070 Ti SUPER 16,376 MiB / compute 8.9, NVIDIA 615.71.09; 30 GiB RAM, ölçüm anında 21 GiB available; Docker 29.8.1; yaklaşık 103 GiB disk boş. uv 0.11.29 ve uv altında Python 3.12.13 var; sistem Python 3.14.7'dir ve değiştirilmeyecek.
- Başlangıçta eksik olan runtime edinimleri artık hazır: ayrı vLLM ve Unsloth ortamları, hash doğrulanmış Qwen3.5-9B ağırlıkları ve ayrı Postgres 16.15 servisi; ayrıntılar `08-runtime-preparation.md` içinde. NVIDIA Container Toolkit doğrulanmadı; native vLLM yolu açık. Kamu verileri edinildi ve kaynak/lisans seçimi kullanıcı kararıyla güncellendi; üretim materyalizasyonu ve gerçek LLM/eğitim/AOS birlikte kullanım ölçümü açık. Mevcut 11434 ve diğer listener'lar korunur; servis başlatmadan önce port yeniden kontrol edilir.
- Başlangıç kaynak profili: P=1, sandbox toplamı en çok 4 GiB RAM ve 2 CPU; ayrı API/Scorer/DB/model bütçeleri ve AOS rezervi ölçülerek yazılacak. En az 20 GiB disk boşluk eşiği korunacak. Bu P=4 kabulü değildir.

## Uygulama sırası

1. Yerel Git `main` ve Luna M0 feature branch; uv ile Python 3.12 paket/kalite kapısı.
2. Ek C'den türetilmiş strict typed harness contract, typed Referee girdisi/kararı, strict JSON-safe reject ve negatif regressions. Maskeli zaman aralığı, PDM/NRM maruziyet ve causal çağrılarını gözden geçir.
3. Harness hash/version, fixture provenance, split/suite/guard ve Scorer/ledger temelini kur.
4. Her görev/faz için ağsız, non-root, kaynak sınırları tanımlı sandbox; ayrı proses ve ayrı output. Güvenilmeyen pickle host'ta açılmaz.
5. Local LLM API/router ve GPU fairness scheduler'ı AOS ile ortak, kimlikli ve sınırlı çağrı arayüzüyle; DB/port/dependency izolasyonu korunur.
6. AOS typed task/tool entegrasyonu, iki tarafta kalıcı idempotency/lease/policy/verification ve iki veritabanına ayrı yazım.
7. CPU fixture koşuları ve bağımsız testler; gerçek veri, vLLM, QLoRA ve AOS birlikte çalışma kanıtlarını ayrı ölç.

## Gerekçeli sapmalar ve varsayımlar

- UID 1000'den UID 1001'e güvenilmeyen sürücü içinden geçiş kaldırıldı. Güvenilir host supervisor ve görev/faz başına ayrı container/PID/IPC, ro rootfs, ağsız çalıştırma kullanılacak; spec §7.M0.3(e) doğrulaması supervisor PID/belleğine erişememeyi ölçecek. Neden: capability drop + no-new-privileges altında uid değiştirme çelişkisi ve paylaşılan çıktı riski.
- İki score çağrısı aynı pickle nesnesi üstünde çalıştırılmayacak. Her çağrı dondurulmuş fit çıktısının kendi sandbox kopyasında çalışacak. Pickle aday sandbox sınırından çıkmayacak; Scorer sayısal çıktı/strict JSON okuyacak.
- PDM maskesi zamanı sıkıştırmayacak; onset yalnız geçerli pencere içinden sayılacak. Sağlıklı maruziyet yoksa görev açık invalid olur. Bu, ek testteki maskeli erkenlik ve NaN karşı örneklerini kapatır.
- Kamu kısa oturumlarında ayrı sürümlü `public_benchmark` embargo/split politikası kullanılacak; SWAPP için bir günlük embargo değişmez. Veri indirme uygunluğu onaylanana kadar synthetic smoke, public kabul sayılmaz.
- Qwen/vLLM ve 24k QLoRA başarı varsayılmıyor; host ve bağımlılıklar hazır olduktan sonra bounded opt-in ölçüm yapılır. Model çağrıları ortak GPU scheduler'dan kısa dilimler alır; tek araştırma run'ına tüm GPU'yu verme veya kalıcı 92% VRAM tahsisi yoktur.
- `KEEP`, Referee v1'de dev seçimidir; nüfus iyileşmesi ya da terfi değildir. Alfa simülasyon bulgusu açık risk kalır ve sürümlü bağımsız doğrulama işidir.
- AOS entegrasyonu ek kapsamla M0'a alınmıştır; AOS çalışma ağacı Git'siz olabilir. Canlı instance değiştirilmeden dar patch'ler hash/backup/diff ve ayrı opt-in oturumuyla doğrulanır.
- Remote/auth yoksa yerel branch/commit/PR metni hazırlanır; PR kabulü açık kalır, merge yapılmaz.

### Native runtime ölçüm güncellemesi — 2026-09-25

Özel user/network namespace ve Unix soketi gerçek host'ta doğrulandı;
`IPAddressDeny=any` tek başına loopback izolasyonu sağlamadığı için ağ sınırı
olarak kullanılmıyor. Adaptörsüz Qwen9B, FP8 per-tensor, eager, 4096 bağlam ve
tek diziyle gerçek S1 yanıtı üretti. Bu tanı profili 32k × 2 veya araştırma
profilinin kabulü değildir. İzole vLLM ortamının derleyici paketleri CUDA 13.2
başlıklarına hizalandı; global CUDA/AOS ortamı değiştirilmedi. Host GCC 16 ile
FlashInfer derlemesi uyumlu olmadığından desteklenen yerel vLLM sampler seçildi.

Model başlangıcı yaklaşık 79 sn sürdü; her çağrıda özel tmpfs cache kurmanın
model değiştirme maliyeti ayrıca ölçülecek. S2 için düşünme/toplam çıktı ve
örnekleme parametreleri açıkça kaydediliyor; kesilmiş yanıt reddediliyor.
Başka GPU tüketicisi belirdiğinde yalnız sahipli test süreçleri durduruldu.
AOS hook'ları ve gerçek birlikte ilerleme hâlâ açık. Ölçümler ve bütün
başarısız denemeler: [18-native-runtime-review.md](18-native-runtime-review.md).

## Veri kullanım profili ve edinim sınırları

- Bağlayıcı amaç profili `noncommercial_research` olarak sabitlendi; kullanıcının son düzeltmesi, önceki ticari geliştirme yanıtını geçersiz kılar. Dataset manifesti, split ve türetilmiş trajectory kayıtları bu profili ve kaynak lisans/atıfını birlikte taşımalıdır. Bu profil ticari dağıtım veya fine-tune izni iddiası değildir.
- Genesis CC-BY-NC-SA 4.0 bu araştırma profili içinde aday kaynak olabilir; lisans ve atıf kökeni her türetilmiş artifact'te korunur. GHL ve SWaT için izin yoktur; belirsiz erişim koşulları çözülene kadar kullanılmaz. GECCO ve CATSv2 CC-BY 4.0 adaylarıdır. Kullanıcı SMD eklenmesini açıkça seçti: TSB-AD-M kapsamı 3 endüstriyel kaynak (Genesis, GECCO, simüle CATSv2) + 1 sunucu telemetrisi (SMD). Dört bağımsız kaynak seçimi tamamlandı; kaynak/split/materyalizasyon doğrulaması açık. Aynı kaynağın dosyaları ayrı küme sayılmaz.
- SKAB loader/split kodu yalnız kaynak bayt hash doğrulaması ve sürümlü public split çekirdeğini sağlar. 35 oturumun tamamında düzensiz zaman aralığı görüldü; salt örnek indeksiyle fiziksel zaman metriği üretilmez. Train-derived cadence, timestamp/gap maskeli sınırlı forward-fill materyalizasyonu ve uygun oturum envanteri tamamlanana kadar SKAB gerçek veri kabulü açık kalır.

## İlk dosya dilimi

`pyproject.toml`, `uv.lock`, `scripts/quality_gate.py`, `harness/contracts.py`, `harness/referee.py`, vendored `vendor/tsb_ad_eval`, `tests/`, `CONTRACT.md`, `harness/VERSION`, `CHANGELOG.md`, bu plan ve `m0-acceptance.md`.

## Güncel uygulama dilimleri — 2026-09-27

Kullanıcının son talebiyle teknik orkestrasyon GPT-6 Astra / high tarafından
yürütülür; model seçimi işin niteliğine göre adaptiftir. Karmaşık 0.34 kapanış
ve bütçe entegrasyonunu Sol / high devraldı; Luna / high eşleşen PostgreSQL
testlerini yürütüyor. Astra dosya sahipliklerini, bütünleştirme sınırını ve
sonraki görev dağılımını belirliyor.

Önceki 0.34 birleştirme sırası (tamamlandı): özel 0.33.2 ile başarısız/bitless
holdout kapanışını tek adayda tamamla; kaynaklar sabitlendikten sonra tam
rezervasyon → bütçe uzlaştırma → application → state → marker zincirini
CPU/PG testleriyle doğrula, ardından imaj eşliğini ve en son zorunlu kalite
kapısını çalıştır. İmaj doğrulamasının güncellediği kilit dosyası son kalite
kapısına dahil edilir; PG ile kapı arasında diğer kaynaklar aynı kalmalıdır.
Sonraki ana işler Director resume/nesil devri/terminal raporu ve
kalan gerçek model, public araştırma ve AOS kabulleridir. Yeni bir kabul
açığı olmadıkça tamamlanmış baseline matrisi tekrar çalıştırılmaz.

Tarihsel 0.32.0 dilimi: Thompson/Director bağlantısı ve kalibrasyonun kalıcı
hücre/süreç sahipliği entegre. Astra kaynak incelemesi, 108 dosyalık imaj
eşliği ve tam kalite kapısı geçti (539 test, yedi komut exit 0).
Kalibrasyonun özel kaynaklarında gerçek PostgreSQL 0023 testi 135 sentetik
receipt ile geçti; gerçek 135 hücre ölçümü ve Thompson v2 PG kanıtı açık.
Özel çalışma ağaçlarında bağımsız `lab baseline` komutunun güvenli durdurma
ve doğrulanmış terminal raporu ile Director sahip nesli/yazım kontrolleri
geliştiriliyor.

Baseline diliminin 17 dosyalık son kaynağı Astra/high tarafından kontrollü
birleştirme için incelendi:
[kaynak incelemesi](review-evidence/baseline-cli-031-source-review.json).
Özel ağacın son geniş odaklı kontrolünde 71 test, ardından son timeout
düzeltmesinde iki test geçti. API'nin eski Planner seal callback'i aynı
şekilde taşınmayacak; Scorer-only durdurma yolu ve 0025 sahipliğiyle birlikte
birleştirilecek. İki Luna worker'ı aynı özel ownership ağacında ayrı dosyaları
üstlendi: Director/API/SQL/recovery ve Scorer job/worker/report. Dondurulmuş
baseline kaynak ağacı korunuyor. Bu kaynak incelemesi gerçek PostgreSQL,
Docker matrisi, stop yarışı veya tam kalite kapısının yerine geçmez.

AOS'un güncel 937 dosyalık public kaynak kopyası ayrı test alanına alındı;
kopya eşliği ve kopyalama boyunca kaynakların sabit kaldığı doğrulandı.
Eski GPU/typed entegrasyon yamasının uygulamadan kontrolü exit 1 verdi:
beş dosyanın bağlamı değişmiş, iki yeni dosya güncel kaynakta zaten var.
Güncel davranışa göre uzlaştırma ve CPU kontrolleri ayrı kopyada yapılacak.
[06-aos-coordination.md](06-aos-coordination.md) kaynak hash'lerini ve
çıktıları bağlar; canlı AOS/GPU testi için bekleyen koordinasyon sürüyor.

Araştırma restart'ı ayrı zorunlu iş: yeni Director neslinin yetkisi, atomik
yazım kontrolleri, kalıcı tek altyapı tekrarı, Scorer öncesi değerlendirme
kökeni ve kesintide dolmaya devam eden run son tarihi uygulanacak.
Mevcut checkpoint bileşenleri tam süreç devamı sayılmıyor.
Kapsam, dosyalar ve gerçek kabul senaryoları
[35-director-restart-scope-review.md](35-director-restart-scope-review.md)
içindedir. Baseline komutu için önerilmiş aşamalı handoff tasarımı,
kullanıcı şartı olarak dayatılmaz; araştırma restart şartı korunur.

EXPLORE için üretim döngüsünün mevcut erken duruşu CPU fixture'ında yeniden
üretildi: enjekte edilen 25 non-KEEP durumunda provider çağrısı olmadan
`explore_family_unverified` dönüyor. Algoritma ailesi, bölüm reset'i ve
açık END geçişi henüz uygulanmadı; mevcut 35 öneri tavanı korunuyor.
[40-explore-current-gap-review.md](40-explore-current-gap-review.md) brief
kapsamını, kaynak bulgularını ve gerçek gözlemin dar sınırını kaydeder.

2026-09-27 devamı: Dondurulmuş 17 baseline dosyası, ana 0.32 kaynakları üzerine
`data/runtime/parallel-m0/integration-033` ağacında metin çatışması olmadan
birleştirildi; 0025 ve migration zinciri henüz birleşik gate'e hazır değil.
Astra'nın [SQL senaryo tasarımı](review-evidence/baseline-ownership-sql-proof-design-033.md)
geçerli A/B owner önkoşullarını ve mutasyonun doğru nedenle reddedilmesini sabitler.
Yeni pidfd gözlemcisinin dört kontrolü gerçek küçük sleeper üzerinde geçti;
Python API eksikliğinden başarısız ilk koşu ve ayrı cleanup gözlemi korunur.
Bu yalnız test gözlemcisi kanıtı; birleşik SQL/API/Docker ölçümü değildir.
[Plan 39](39-baseline-ownership-verification-plan.md) kapsamı ve interpreter
sınırını kaydeder. Ürün runtime'ı 0.32.0 ve kabul özeti 10/8/4 olarak kalır.
