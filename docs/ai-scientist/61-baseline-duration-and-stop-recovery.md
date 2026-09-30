# Çok görevli baseline süresi ve durdurma kapanışı

2026-09-30. Ana sürüm 0.38.0; aşağıdaki süre düzeltmesi ayrı 0.39 çalışma
kopyasında test edildi, henüz ana sürüme alınmadı.

## Gerçek araştırma sonucu

27 görev / üç baseline / üç seed için 243 ölçüm planlayan koşu, 18 gerçek
baseline ölçümünden sonra `stop_requested` oldu. 18 Scorer işi tamamlandı;
hiçbir aday ölçülmedi. Başlatıcı ve aşama sürücüsü exit 1 verdi. Alt CLI
ve canonical owner exit 0 ile `recovery_required` döndürdü; bu normal çıkış
başarılı araştırma değildir.

Gözlenen istisna `CandidateExecutionRejected`; yaklaşık 600 saniyede
sona ermesi ve kaynakta bütün görevlerin tek 600 saniyelik rezervasyona
sığdırılması süre sınırıyla ilişkilidir. İstisnanın daha alt neden kodu
ölçülmedi; `whole_suite_seed_budget_exhausted` diye etiketlenmedi.

[Gerçek başarısız koşu özeti](review-evidence/public056-research-dispatch-failure-summary.json).
135 kalibrasyon ölçümü, iki eski attempt ve kaynak/input hashleri korundu.
Eski deadline ve rezervasyon değiştirilmedi; koşu otomatik tekrar edilmedi.

## Test edilmiş süre düzeltmesi

Yeni rezervasyon yalnız yeni bir baseline/seed başlatılırken
`min(görev sayısı × görev başına sınır, kalan run bütçesi)` olur.
Görev başına 600 saniyelik sınır ve toplam 14.400 saniyelik üst sınır
korunur. Devam sırasında kayıtlı rezervasyon kimliği/süresi ve başlangıcı
yeniden kullanılır; duraklama yeni süre yaratmaz.

Beş CPU regresyonu geçti: iki görevlik açık kısa sınır, tek görevlik eski
sınır, 27 görev × 35 sanal saniyede tam ölçüm, yetersiz kalan bütçe ve
120 saniye yaşlanmış eski rezervasyon. Sonuncusu 14 mocked ölçümden sonra
özgün 600 saniyede durur. SQL/Docker bu testlerde mock; gerçek veri
araştırmasının veya holdout kabulünün tamamlandığı iddia edilmez.

[Kaynağa bağlı beş test](review-evidence/baseline039-regression-summary.json).

## Kalan kapanış

Mevcut durdurma protokolü henüz ölçülmüş fakat calibration freeze
tamamlanmamış baseline araştırmasını terminal rapora kapatamıyor.
Ayrı kopyada dar SQL/Python değişikliği hazırlandı ve 88 odaklı CPU testi
geçti (50 yeni durdurma, beş süre, beş calibration receipt ve 28 mevcut
durdurma regresyonu; gerçek exit 0). SQL/Docker bu testlerde mock.
İncelemede yeni invocation RPC’sinin son sorgusuna ayrıca aynı run/job
kimliği koşulu eklendi. Son kaynakta 93 test exit 0: ek beş test migration
içinden alınan gerçek SELECT’i SQLite üzerinde geçerli, farklı run/task,
canlı claim token ve yanlış completion durumlarıyla sınar; PostgreSQL
kanıtı değildir. [Son 93 test](review-evidence/stop039-final-focused-summary.json).
İlk 88 test özeti kendi eski kaynak hashlerine bağlı olarak korunur.

[Kaynağa bağlı 88 test](review-evidence/stop039-focused-summary.json).

Önerilen kapanışta: mevcut ölçümler,
job/source/generation bağları ve dead-child doğrulaması korunacak;
calibration veya gelecekteki görevler uydurulmayacak. PostgreSQL, birleşik
gate, imaj ve gerçek kapanış kanıtı oluşana kadar bu iş tamamlanmış değildir.


İlk ayrı PostgreSQL kopyası denemesi özgün kayıtların salt okunur snapshot
aşamasında `ProgrammingError` / exit 1 verdi. Kopya konteyneri ve dump
oluşmadı; ölçülen SQL nedeni henüz bilinmiyor. Ana DB ve eski koşu
yazılmadı; başarısız deneme korunuyor.
[İlk gerçek kopya denemesi](review-evidence/public056-stop039-clone-r1-failure-summary.json).

Sonraki salt okunur teşhis SQLSTATE `42501` ile `task_completions` okuma
yetkisini doğruladı. Test rolünün yetkileri genişletilmiyor; tamamlanma
snapshot’ı yalnız özgün konteynerin mevcut yönetici kanalında açık
`READ ONLY` transaction ile alınacak. R2 sürücüsü ve katalog geri alma
yaması hazırlanıyor; henüz yeniden çalıştırılmadı.
[Ölçülmüş teşhis](review-evidence/public056-stop039-clone-r1-diagnosis-summary.json).

## İkinci gerçek kopya denemesi

R2, 0033 geri alma öncesindeki tüm tablo hash kontrolünde SQLSTATE `57014`
ile durdu (exit 1, 60,742 saniye). PostgreSQL günlükleri statement timeout
ile iptal edilmiş cursor FETCH gösteriyor; hangi tablo olduğu ölçülmedi.
Katalog geri alma veya gerçek durdurma tamamlanmış sayılmaz. Kendi kopya
konteyneri durdurulup korundu.

Ardından ayrı salt okunur doğrulama gerçek exit 0 verdi: özgün iki
kalibrasyon attempt’i, 135 ölçüm, input hashleri, bütçeler/deadline’lar
ve 18 araştırma ölçümü/işi/tamamlanması R2 öncesiyle birebir aynı.
Yeni test sürücüsü DB’de bütün JSON satırlarını sıralamak yerine
belleği sınırlı satır-hash sıralamasını hazırlıyor; araştırma bütçesi
ve 5 saniyelik DDL/lock sınırı artırılmıyor.
[R2 ve özgün veriyi koruma kanıtı](review-evidence/public056-stop039-clone-r2-failure-summary.json).


## Üçüncü gerçek kopya denemesi

R3 ayrı PostgreSQL kopyasında exit 0 verdi: 37 gerçek kontrol, dış
başlatıcıda 30,321 saniye. 0032 kataloğu yakalandı; 0033 yükseltmesi,
koşullu geri alma ve gerçek Migrator rolüyle yeniden yükseltme tamamlandı.
Director yetkisi, en çok 120 saniyelik drained stop kapsamı ve
job/generation/pin/output/tamamlanma bağları sınandı; gerçek üretim
durdurma/finalize yolu kopyada geçti.

Özgün 135 kalibrasyon ölçümü ile 18 araştırma ölçümü/işi/tamamlanması,
bütçeler, deadline’lar ve generation kayıtları öncesi/sonrası birebir aynı.
Ana DB’ye veya eski koşuya yazılmadı. Yalnız kendi kopya konteyneri
durdurulup korundu; R1/R2 başarısız kanıtları korunuyor.

Bu kanıt tek bir mevcut, ölçülmüş fakat tamamlanmamış baseline kapanışını
kapsar. Rehearsal harness sürümü **0.38.0** idi; 0.39 sürüm/imaj/gate
kabulü veya ana sürüme geçiş kanıtı değildir. M0.10 açık kalır; yeni
araştırma, holdout, yerel model veya AOS birlikte çalışma kabulü tamamlanmış
sayılmaz.

[Redakte edilmiş R3 başarı özeti](review-evidence/public056-stop039-clone-r3-success-summary.json).
Özgün özel receipt SHA-256:
`a3abfddf4101bbcef558babeb13815b17b2a94566f8b5859d4e9cff7d4613e9b`.


## Özel 0.39 imajı ve birleşik gate

Özel 0.39 imaj doğrulaması exit 0 verdi (6,144 saniye); parity kontrolleri
geçti, kullanıcı `10001:10001`. Son birleşik CPU gate’in yedi komutunun
gerçek exit kodları sıfır: pytest 1.163 geçti, yedi skip ve 120 deselect.
359 dosyalık öncesi/sonrası kaynak envanteri aynı; bunun 301’i runtime,
58’i HEAD’den alınan test dokümanıdır. Son runtime dosyaları başarılı
imaj kanıtıyla birebir aynı; yalnız test dokümanı ekleri için imaj
yeniden oluşturulmadı.

İlk gate dört eksik doküman yüzünden collection aşamasında exit 1 verdi.
İkinci gate kalan iki eksik HEAD dokümanı yüzünden 26 fail ile exit 1
verdi (1.137 passed, yedi skip, 120 deselect). İki başarısız gate’in
sürücüleri, receipt ve günlükleri korunur; test beklentileri değiştirilmedi.
Eksik yayımlanmış fixture dosyaları eklendikten sonra üçüncü gate geçti.

[Hashlere bağlı gate/imaj ve başarısızlık geçmişi](review-evidence/release039-gate-image-summary.json).
Bu özel aday kanıtıdır: ana sürüm bu kayıt anında **0.38.0**.
Dağıtım veya yeni gerçek araştırma kabulü değildir; M0.10 açık kalır.


## Ana sürüme gerçek 0.39 dağıtımı

2026-09-30: gerçek dağıtım **session 5925 / exit 0** ile tamamlandı.
Ana harness artık **0.39.0**, DB head `0033_measured_baseline_stop`.
Sekiz incelenmiş dosya ve üç işlevlik atomik migration uygulandı;
ana dosyaların receipt hashleri ve 149 imaj kaynak dosyasının parity
hashleri ayrı salt okunur kontrolde eşleşti.

Dağıtım öncesi salt okunur preflight exit 0 verdi. Dağıtım receipt’inde
47 tablonun 234.073 satırı, özgün bütçe checkpoint’leri, deadline ve
generation kayıtları öncesi/sonrası aynı. Aktif Scorer işi sıfır; trigger
ve view katalogları korunmuş. Yalnız üç sahipli CPU servisi durdurulup
yeniden aktif oldu; tünel kimliği aynı. Tam DB restore kullanılmadı.

[Redakte edilmiş gerçek dağıtım özeti](review-evidence/release039-deployment-summary.json).
Önceki özel aday ve 0.38 rehearsal kayıtları tarihsel sürüm bağlarını
korur. Bu dağıtım eski başarısız araştırmayı tamamlamaz; bilimsel/GPU/model,
holdout, AOS birlikte çalışma ve M0.10 kabulü açık kalır.


## Ayrı 0.40 çoklu baseline kapanış fixture’ları

Özel 0.40 çalışma kopyasında çoklu baseline stop kapanışı için
**124 CPU fixture testi / gerçek exit 0** ölçüldü (pytest 1,51 saniye;
sahipli sınırlı test unit’i 1,989 saniye, 163,5 MB bildirilen tepe bellek,
swap sıfır). 301 dosyalık runtime fence ve ana/özel 0.39 kaynakları
korundu. Üç üretim dosyası ve test dosyası hashleri kanıta bağlıdır.

İlk denemenin **121 passed / 3 failed / exit 1** sonucu korunur. Suite
fixture dosyası yanlışlıkla CAS artifact store içine konmuştu; yalnız
fixture yolu düzeltildi. Üç üretim dosyasının hashleri değişmedi.

[Kaynağa bağlı 124 fixture özeti](review-evidence/stop040-focused-summary.json).
Bu testlerde DB ve process-death mock’tur: gerçek 0034 PostgreSQL
katalog/yetki/geri alma veya çoklu ölçülmüş baseline terminal kapanışı
kanıtı değildir. 0.40 tam gate/imaj/ana dağıtımı yapılmış sayılmaz;
ana sürüm 0.39 ve bilimsel/GPU/AOS/holdout/M0.10 açık kapsamı korunur.
