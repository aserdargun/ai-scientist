# Holdout entegrasyonu — Astra incelemesi ve devam eden düzeltmeler

Tarih: 2026-09-27. Önceki hedef turu ilerleme sağladı: yerel kontrol arayüzü
`1d8b07d` ile kaydedildi; son kalite kapısı 389 testle geçti. M0'ın bütünü
tamamlanmış değildir; kabul sayıları 10 geçti / 8 kısmi / 4 açık kalır.

## İncelenen kaynak ve kanıt sınırı

Dondurulmuş Luna/high çalışması `data/runtime/parallel-m0/holdout-025`,
taban commit `221e5d94ceb17e7ef1f933f55db60e8d02987a02`. Kaynak/envanter
hash'leri yeniden doğrulandı. Tarihsel kayıtlar:

- `review-evidence/holdout-025-delivery-inventory.json`
- `review-evidence/holdout-025-vertical-review-current.json`
- `review-evidence/holdout-025-vertical-review.json`

Bu kaynakta ayrı PostgreSQL 16 ve gerçek Docker/Scorer worker ile **2 canlı
test**, ayrıca **44 hedefli test** başarılıydı. Canlı akış sentetik EVT ve
run-end yoludur. **Farm B, gerçek 10 KEEP tetiklemesi ve canary sızıntısı
kabulünü kanıtlamaz.** Tüm eski test sonuçları eski kaynaklara aittir.

## Astra/high bulguları

| Bulgu | Gerekli düzeltme ve kanıt |
|---|---|
| Periodic holdout süresi restart'ta geri kazanılıyor | İşten önce kalıcı bütçe rezervasyonu; terminalde tüketim uzlaştırması; unresolved rezervasyonun muhafazakâr restorasyonu. Sonuç sonrası/sonraki öneri öncesi crash testi. |
| 10 KEEP sonrası state anahtarı run-end SQL koşuluna uymuyor | Gerçek state anahtarını, hash'ini ve uygulama marker zincirini doğrula; ilişkili marker'ı kabul ederken ilgisiz yeni event'i reddet. Gerçek PG periodic → run-end testi. |
| Proposal limitindeki son periodic bit crash sonrası atlanıyor | Döngü bitişinden önce pending uygulamaları uzlaştır; ordinal 10'da kalmış reverted bitinin rollback'i yeni sorgudan önce gerçekleşsin. |
| Ölen worker rezervasyonu kalıcı running bırakıyor | Scorer tarafında eski exact generation/cgroup'un öldüğünü doğrulayan terminal hata kurtarması; kotayı iade etme, sorguyu tekrarlama, bit uydurma. Kill-after-claim ve idempotent resume testi. |
| Stop sonrası yeni holdout fazları başlayabiliyor | Her fit/score öncesinde run aktifliğini denetle; yalnız kaydedilmiş worker generation'ını temizle. |

Normal akıştaki atomik kota kilitleri ve holdout metriklerinin Scorer içinde
kalması incelendi. Bu, yukarıdaki crash/stop yollarını doğrulanmış yapmaz.
Frozen kaynak henüz entegrasyon onayı almadı.

## Çalışma düzeni

- Thinker: **GPT-6 Astra/high**; mimari ve düzeltmelerin bağımsız incelemesi.
- Uygulama: **GPT-6 Luna/high**; güncel `1d8b07d` tabanlı ayrı `holdout-030`
  çalışma ağacına dar taşıma ve düzeltmeler. 0.28 eğitim komutları, 0.29
  harness reddi/strateji ve yerel arayüz korunur.
- Migration kimliği `0019_bounded_holdout`, parent `0018_public_task_semantics`.
  Canlı ana DB'ye uygulanmış değildir.
- Ana runtime şu anda 0.29'dur. Entegrasyon, yeni harness sürümü/imaj eşliği
  ve güncel kaynağa bağlı kalite kanıtı olmadan tamamlanmış sayılmaz.
- AOS V4 sürücüsündeki bağımsız düzeltmeler CPU sözleşme testleriyle ilerler.
  Gerçek GPU/model/broker/AOS denemeleri bekleyen koordinasyonun dışına çıkmaz.

Sonraki kabul kanıtı tam kapsamı korur: CARE Farm B'nin gerçek private
yüklenmesi, 10 gerçek KEEP sonrası kontrol, koşu sonunda kontrol, atomik
20/run ve 100/suite-version kotası, rollback ve ajan görünümünde yalnız bit;
canary hiçbir trajectory/bulgu/diff içinde görünmemelidir. Farm A loader'ı
Farm B kabulü yerine kullanılamaz; mevcut Farm A status politikası Farm B'ye
incelemeden taşınmaz. Farm C sealed kalır.

## Bağımsız AOS V4 sürücü düzeltmesi

`review-evidence/review_aos_lab_coexistence_v4.py` artık bootstrap/readiness
başarısını beklemeden exact unit generation'ını yakalar. `systemd-run`
istemcisinin `TimeoutExpired`/`OSError` durumu da yakalama ve kendi neslini
temizleme yoluna girer. Herhangi bir drain başarısızlığı başarı sonucunu ezer.

Birlikte ilerleme denetimi tarihsel ticket toplamına dayanmaz: aynı boot,
Lab run ve AOS görev aralığında canlı ticket → done geçişini ve gerçek provider
ilerlemesini ister. Lab aralığın iki ucunda da running olmalıdır. AOS foreground
görevi doğrulanmış kalibrasyon ve aktif provider denemesi sonrasında başlar.

Root'un dondurulmuş kaynak kontrolü gerçek **exit 0**: **11 CPU sözleşme
testi** ve hedefli Ruff başarılı. Kaynaklar test boyunca değişmedi;
`review-evidence/aos-lab-coexistence-v4-cpu-review.json` komut, ham çıktı ve
hash'leri taşır. Testlerde systemd/AOS/API yanıtları fixture'dır; gerçek AOS,
GPU, model veya DB çalıştırılmaz. Worker'ın ilk mocked-clock hatası ve sonraki
düzeltmesi ayrı sınırlama notunda belirtilir; başarısız deneme başarılı sayılmaz.

V4 hâlâ gerçek birlikte çalışma kabulü değildir. Lab başlangıcı HUMAN-owned
typed API'dir; aynı oturumda Decider gözlemi bulunması **M0.AOS.5 için modelin
Lab işini başlattığını kanıtlamaz**. Bu sınır sürücünün kendi sonucunda da yer alır.
Gerçek çalıştırma bekleyen GPU/AOS koordinasyonu sonrasında ayrıca doğrulanır.

İkinci Luna worker holdout recovery işini `0020_holdout_recovery` migration'ı
ile ayrı yürütür; parent 0019'dur. Ana DB'ye uygulanmamıştır. Aşağıdaki
geçici PostgreSQL provası yalnız kaydedilmiş kaynak snapshot'ını kapsar. Daha önce planlanan
baseline-handoff migration'ı, bu numarayı yeniden kullanmadan sonraki boş
revision'a taşınmalıdır.

## Bu hazırlık commit'inin kalite kapısı

Ana checkout'ta **session 41119 / exit 0**: yedi komut başarılı, **389 passed /
7 skipped / 13 deselected**, core strict mypy 84 kaynak ve Pylint 9.40.
`review-evidence/aos-v4-holdout-preparation-quality-gate-binding.json` V4
dosyalarının değişmediğini ve ana harness'in aynı 76 dosyalık 0.29 kimliğinde
kaldığını kaydeder. Sonuç `evidence/quality-gate-aos-v4-holdout-preparation.json`
içindedir. Bu kapı private `holdout-030` çalışma ağacının kabulü değildir.

Staged whitespace kontrolü **exit 2**: yalnız ham gate çıktısındaki altı
deprecation-warning satırının sonda boşluğu vardır; ham kanıt değiştirilmedi.
`*.stdout.txt` hariç kaynak/belge kontrolü **exit 0**.

## Güncel tabana ilk taşımanın bağımsız incelemesi

`holdout-030` ilk teslimi `1d8b07d` tabanında donduruldu. Yama SHA-256
`a32926b8a723f50a24194e9ffe42c6ff1960dba01091191d40e1efcf2b062a3d`.
Worker'ın kaynak hash'leri, modül kökleri ve komut sonuçları
`review-evidence/holdout-030-first-handoff.md` içinde korunur: 16 hedefli
CPU testi, Ruff, mypy ve derleme başarılı. Eşzamanlı Scorer düzenlemesi
sırasında oluşan privacy test collection hatası da kayıtlıdır. Güncel
PostgreSQL/Docker kabulü yoktur.

Astra/high bu ilk taşımanın entegrasyonunu onaylamadı. Ayrıntılı kayıt:
`review-evidence/holdout-030-astra-first-review.json`.

| Yeni bulgu | Gerekli düzeltme |
|---|---|
| JSON'dan okunan UUID'ler strict Python-dict doğrulamasında reddediliyor | Gerçek run-end wrapper'ında JSON doğrulama yolu ve regresyon testi |
| Periodic + run-end budget birleşik state anahtarı SQL'de reddediliyor | Tam birleşik anahtar/hash/marker bağını koruyan kabul ve 10 KEEP → run-end testi |
| State yazıldıktan sonra marker öncesi crash, canlı bütçeyle yeniden hesaplanıyor | Uygulama kaydındaki sabit bütçe ile deterministik state kurtarma |
| Elapsed süre ölçülen süreden büyükse unresolved bütçe tüketimi kaybolabiliyor | Önceki measured/elapsed üst sınırına tam bilinmeyen rezervasyon süresini ekleme |
| Intent commit'i ile SQL rezervasyonu arasındaki crash kalıcı hataya dönüyor | Aynı intent ve eski bütçeyle idempotent kurtarma veya kalıcı bitsiz başarısızlık |

Luna/high ikinci düzeltme turunu ayrı çalışma ağacında sürdürüyor. İlk
teslimin uygulanmış özellik listesi bu açık bulgular ışığında okunmalıdır;
16 test bu yolları henüz doğrulamaz. Recovery worker'ın 0020/Scorer kodu
ayrı inceleme bekler. Ana runtime ve M0.10 durumu değişmedi.

Farm B kaynak taraması tamamlandı; kaynak doğrulaması, seçilen eğitim ve
sağlıklı referans politikası ve henüz ölçülmemiş kabul maddeleri
`33-care-farm-b-source-review.md` içinde tutulur.

Eksik run-end rezervasyonu için Astra'nın seçtiği sözleşme, 0020'de ayrı
`lab.holdout_run_end_fences` kaydıdır. Aynı run kilidi altında immutable intent
ve tüketimi uzlaştırılmış bütçe checkpoint'i doğrulanır; yeni sorgu, kota iadesi
veya sahte reservation UUID üretilmez. INSERT guard sonraki rezervasyonu
engeller. Primary worker ayrı `RunEndAdmissionFailure` tipiyle son onaylı
şampiyona dönüş ve insan incelemesi durumunu bağlar. Tablo/RPC/guard recovery
worker'a, Director bağlantısı primary worker'a aittir; henüz doğrulanmış
uygulama değildir.

## Geçici PostgreSQL'de migration ve rol provası

Root, ayrı bir PostgreSQL 16 konteynerinde kaynak snapshot'larını denedi.
İlk iki deneme gerçek **exit 1** ile sonuçlandı: SQLAlchemy, 0019 içindeki
regex'in `:keep_interval` bölümünü bind parametresi olarak yorumladı. İlk
düzeltmeden sonra marker regex'inde kalan aynı sorun ikinci denemede
görüldü. Literal kolonların regex'te `[:]`, marker üretiminde `chr(58)` ile
ifade edilmesi sonrası üçüncü deneme **session 81558 / exit 0** ile geçti.

| Deneme | Gerçek sonuç | Kanıt |
|---|---|---|
| İlk | session 9230 / exit 1 | `review-evidence/holdout-030-migration-smoke-23579382eaa8.json` |
| İkinci | session 91778 / exit 1 | `review-evidence/holdout-030-migration-smoke-6f173f6d512b.json` |
| Düzeltilmiş snapshot | session 81558 / exit 0; head `0020_holdout_recovery` | `review-evidence/holdout-030-migration-smoke-83b698710aa4.json` |

Başarılı snapshot'ta 0019 SHA-256
`bda4cdb44f65390fd8b8c967639c3d6850ac623a98be9d494d20c82942309e96`,
0020 SHA-256
`51105d1d3b6bf3169df327159da4857a5ffb2ad466e64ed6523d7758212a6e6c`.
**36 kontrol** başarılıdır: 18 katalog yetki denetimi ve Director, Planner,
Scorer olarak ayrı girişlerle 18 gerçek RPC/SELECT çağrısı. RPC çağrıları
olmayan kaynaklar üzerindedir; yetkisiz rollerin reddini ve güvenli hata
yollarını doğrular. Gerçek reservation/fence yarışı, holdout skoru, ölü worker
kurtarması veya Director crash kabulünü doğrulamaz. Sonradan değişen 0020
ve yeni unclaimed-recovery RPC'si bu sonucun kapsamında değildir.

Sürücü `review-evidence/review_holdout_030_migrations.py`; her denemenin
snapshot ve çalıştırılan sürücü hash'i kayıttadır. Yalnız bu provaya ait
benzersiz etiket/ID ile doğrulanan konteynerler kaldırıldı; geçici kimlik
bilgileri silindi. Her üç kayıtta temizlik başarılıdır. PostgreSQL 512 MiB,
0.5 CPU, 64 PID, swap kapalı ve loopback'te dinamik portla sınırlandı. Ana
Lab DB'si, canlı AOS ve GPU servisleri kullanılmadı.

## Recovery incelemesinden kalan riskler

Astra/high ilk recovery diliminin entegrasyonunu onaylamadı:

- Docker temizliği başarısız veya belirsizken SQL'de terminal hata yazılmamalı;
  eski owner marker'ı P=1 kilidi altında yeniden doğrulanmalı.
- Yetkili exact-generation stop sonrasında systemd unit'i toplanmış olabilir;
  PID/start/boot ve cgroup yokluğu doğrulanarak bu yol tamamlanabilmeli.
- Director'ın mevcut reserved/running kaydı gerçek recovery adapter'ına
  ulaşmalı; yeni worker veya yeni kota oluşturmamalı.
- Worker claim'inden önce ölümde kalan reserved kayıt için launcher ile aynı
  lifecycle kilidi, sınırlandırılmış yokluk kanıtı ve Scorer'a özel atomik
  başarısızlık geçişi gerekir. Kaydedilmemiş süreç kimliği uydurulmamalı.

Luna/high düzeltmeleri ayrı kopyada sürüyor. Bu bölümdeki migration/rol
başarısı recovery onayı değildir; ana runtime 0.29 ve M0.10 açık kalır.

## Recovery R2 ve ikinci gerçek PostgreSQL provası

Recovery R2'nin dondurulmuş 0020 SHA-256'sı
`2e33100b37db9835191b4d24d50bb879f999a816ca16c38b9cf1240a625d7e65`.
Astra/high bu kaynaklarda önceki Docker temizliği, collected-unit,
unclaimed-reservation ve stop bağlantısı engellerinin giderildiğini doğruladı.
P=1 kilidi altında marker doğrulanır; temizlik başarısız/belirsizken terminal
CAS yapılmaz. API stop yanıtından sonra sınırlı Scorer recovery çağrısı vardır.

Luna'nın dar kaynak diliminde 47 CPU testi, Ruff/format ve derleme başarılıdır.
Daha geniş denemede 85 test geçerken primary'ye ait bir timer fixture'ı hata
verdi; bu deneme başarı sayılmadı. R3'te son kaynaklarla tekrar doğrulanacak.

Root yeni migration snapshot'ını ayrı PostgreSQL'de çalıştırdı:
**session 67594 / exit 0**, 48 rol/olmayan kaynak kontrolü, head
`0020_holdout_recovery`. Yeni Scorer-only unclaimed/list RPC'leri kapsamda;
kendi konteyneri kaldırıldı ve kimlik bilgileri silindi. Kaynaklar deneme
boyunca sabitti. Kanıt
`review-evidence/holdout-030-migration-smoke-7f1659ef7191.json`, sürücü
`review-evidence/review_holdout_030_recovery_migrations.py`.

Bu 48 kontrol migration/grant ve olmayan kaynak yollarını kanıtlar. Gerçek
pozitif reservation/fence/CAS yarışları, worker öldürme, systemd/Docker drain
ve tam 10 KEEP/Farm B/canary kabulü hâlâ açık. Source review bu eksikleri
tamamlanmış saymaz.

## Primary R2'nin kalan üç engeli ve R3 sözleşmesi

Astra, ilk R2 loop hash'i
`46e4bf6ce10b71634fb0131c271a6a2da4f3b692987cfda00b97046b7031ab49`
üzerinde önceki UUID/anahtar/bütçe düzeltmelerini doğruladı; aşağıdakileri açık
bıraktı. R2 yaması ve review notu tarihsel teslim olarak korunur.

1. Admission-failure replay, özgün adayı rollback sonrası champion ile
   karşılaştırıyor. Aday, hash ile bağlı prior state/intent'ten doğrulanmalı;
   farklı onaylı champion'a dönüş sonrası state/marker crash'leri aynı sonucu
   üretmeli.
2. Intent checkpoint'i yazıldıktan sonra SQL intent kaydı öncesi crash'te
   SQL intent satırı yoktur. 0020 fence'i bu satıra bağlı olduğu için bu yolu
   terminalize edemez.
3. Fresh wall-budget sıfırsa CLI'nin budget-exhausted yolu genel hata verir.
   Yeni süre/sorgu/kota üretmeden kalıcı manual-review/rollback gerekir.

R3 için ayrı `0021_run_end_unavailable` sözleşmesi seçildi: run ID'ye bağlı
ayrı fence, gerçek checkpoint/state/bütçe kimlikleri, sabit neden kodları,
Director-only write/read RPC ve run-row kilidi. SQL intent ve reservation
yokluğu doğrulanır. Sonraki intent, run-end reservation ve experiment
admission aynı kilit altında engellenir; eski işlerin terminal sonuçları
bozulmaz. Director checkpoint → fence → typed application → saf rollback →
marker sırası izlenir. Sahte reservation/intent ID, kota iadesi veya yeni
model çağrısı yoktur.

Bu R3 mimari kararıdır; uygulama, yeni SQL pozitif/crash/race testleri ve
entegrasyon onayı henüz tamamlanmadı. Önceki baseline-handoff taslağı 0021'i
kullanmayacak; o iş bir sonraki boş migration kimliğine taşınacak.


## R3 gerçek SQL sonucu ve Astra yeniden başlatma incelemesi

Yeni izole PostgreSQL provası **session 96814 / exit 0** ile tamamlandı:
`test_postgres_holdout030_recovery.py` içindeki bir canlı test ve 48 ek
rol/olmayan kaynak kontrolü geçti. Test; unclaimed/running kurtarma CAS,
aynı isteğin tekrarı, eski worker kimliğinin reddi, stop sonrası admission
reddi, değişmeyen kota ve eşzamanlı 0020 fence/reservation yarışını gerçek
SQL üzerinde denedi. Worker kimliği ve skor fixture'dır; gerçek süreç ölümü,
Docker drain veya tam Director wrapper kabulü değildir. Yarış tek koşuda
iki olası kazananın yalnız birini ölçer.

Snapshot head `0021_run_end_unavailable`, migration SHA
`561c02c501231e28398f9254ef2f52dc419a1a09ab33bd5bf83d414ac4700896`.
Kaynak snapshot'ı değişmedi; yalnız bu provanın konteyneri ve geçici DB
kimlik bilgileri temizlendi. Servis 4,647 s / 1,715 CPU s,
171,6M raporlanan tepe bellek ve sıfır swap ile tamamlandı.
Kanıt: `review-evidence/holdout-030-recovery-sql-4390d99a595f.json`.

Önceki başarısız SQL denemeleri korunur: testte calibration sırası,
0021 trigger'ın experiments satırında bulunmayan `trigger_kind` alanına
bakması, pytest tmpfs yolunun 20 GiB rezervini karşılamaması ve test JSON
literalinin SQLAlchemy bind olarak yorumlanması. Trigger kaynakta,
diğer üç sorun test/runner katmanında düzeltildi; korumalar gevşetilmedi.

Astra/high, R3'te iki yeniden başlatma engeli buldu:

- Rollback state tekrarında yeni sequence üretilmesi gerçek immutable
  journal receipt'iyle çakışıyor; aynı kayıt doğrulanarak yeniden kullanılmalı.
- Donmuş unavailable intent'in bütçesi geri yüklenmiyor; CLI proposal
  döngüsü terminal uzlaştırmadan önce yeni işe erişebiliyor. Terminal intent
  veya fence, provider/proposal ve yeni rezervasyondan önce ele alınmalı.

Önceki candidate/approved-champion karşılaştırması düzeltilmiş. Luna/high R4
bu iki engeli gideriyor; R3 kaynak onayı verilmedi. Ayrıntı ve hash'ler:
`review-evidence/holdout-030-astra-r3-review.json`. Ana runtime ve kabul
sayıları değişmedi.


Ana 0.29 runtime + bu kayıtların commit öncesi tam kalite kapısı
**session 23670 / exit 0**: yedi komut başarılı, **389 passed / 7 skipped /
13 deselected**, strict mypy 84 dosya ve Pylint 9,40. Bu kapı ayrı
holdout-030 kaynaklarının tam entegrasyon kapısı değildir. Komut/kanıt bağı
`review-evidence/holdout-sql-care-registration-quality-gate-binding.json`.


## 0.30 ana kaynak entegrasyonu ve son kapı

Astra/high R4 incelemesinde iki önceki mekanizma düzeldi; yeni genel
`payload.run_id` filtresinin eski proposal/seed bütçe kayıtlarını atlaması
saptandı. R5 yalnız yeni unavailable-intent formatında bu alanı zorunlu
kılar; SQL run ID ve lease ile bağlı eski formatlar korunur. Legacy
proposal/seed ve gerçek sıfır süreli başlangıç regressions eklendi.
Astra kaynak entegrasyonunu onayladı.

**0.30.0**, incelenen Director/CLI/reporting/API, Scorer holdout/recovery,
üç immutable migration ve Farm B yükleyici/adapter'ını ana çalışma dalına
aldı. Mevcut çalışan veritabanına migration uygulanmadı; SQL provaları ayrı
geçici veritabanlarında çalıştı. Henüz gerçek Scorer süreç ölümü veya bilimsel
holdout skorlama kabulü verilmedi.

| Son kanıt | Sonuç |
|---|---|
| Gerçek 0021 RPC'leri | session 89710 / exit 0; iki neden ve sıralı admission koşulları, 3 test +72 kontrol |
| Gerçek journal state/marker tekrarları | session 1127 / exit 0; 1 test +72 kontrol |
| Ana 0.30 wrapper'ın son tekrarı | session 51300 / exit 0; 2 test +72 kontrol |
| Son sandbox image | session 19586 / exit 0; 103 runtime dosyasında byte eşliği |
| Tam kalite | session 70892 / exit 0; yedi komut başarılı |
| Pytest | 487 passed / 7 skipped / 22 deselected |
| Strict mypy / Pylint | 94 dosya / 9,37 |

Son wrapper testi gerçek PostgreSQL ve `DirectorRunLease` ile aşağıdakileri
çalıştırdı: eski pozitif bütçe ve proposal limiti altındaki state üzerinden
frozen exhaustion intent, SQL fence, onaylı champion'a rollback, state
sonrası marker öncesi enjekte edilmiş hata ve marker yazıldıktan sonraki
bir başka tam `loop.run()` başlangıcı. Sonuç, state/marker receipt'leri,
checkpoint sayısı ve reservation/kota/result sayıları değişmedi. Yeni
provider çağrısı veya rezervasyon yapılmadı. Bunlar **sentetik ledger ve
enjekte edilmiş exception sınırlarıdır**; işletim sistemi süreç öldürme
veya aday skorlama değildir.

Son testte ilk callback imzası hatalıydı (session 20784 / exit 1);
düzeltilen ön sürüm session 35398 / exit 0 ile geçti. Astra'nın istediği
marker sonrası tam başlangıç kontrolü eklenerek son session 51300
çalıştırıldı. Başarısız ve ara kanıtlar saklandı.

İlk tam kapı session 63320 / exit 1: üretim `assert`, sessiz failure-publication
exception'ı ve eksik review-helper dosyası yakalandı. Zorunlu reservation
kontrolü açık `RuntimeError` oldu; ikinci hata asıl exception'a sabit,
private ayrıntı içermeyen bir not ekler ve asıl hatayı tekrar yükseltir.
Astra bu dar farkları onayladı. Eksik helper önceki R2 hash'iyle alındı;
Bandit için yeni suppression veya kalite eşiği gevşetmesi yapılmadı.

Son image:
`sha256:2ad07bb30f69a9a97ed05acbc402ebafacd06566209d0e70819432c62540484e`.
Harness fingerprint **83 dosya**:
`d5c306daeb0d39e61ba982b257bee3d481e0219d355ad1f8a21e3dce8bd014ea`.

Kaynak ve komut bağları:
`review-evidence/holdout-integration-030-quality-gate-binding.json`,
`review-evidence/holdout-integration-030-image-final.json`,
`review-evidence/holdout-030-recovery-sql-22b411132ba7.json`,
`review-evidence/holdout-030-astra-final-delta.json`.
Gerçek worker/container drain, Farm B'nin ölçülmüş kalibrasyonu/skorları,
10 KEEP/run-end/canary tam kapsamı ve gerçek Qwen/AOS birlikte çalışma
kabulleri açık kalır. Kabul sayıları değişmedi.
