# Baseline ve Director sahipliği — özel 0.33 entegrasyonu

Tarih: 2026-09-27. Thinker: GPT-6 Astra / high; uygulama: GPT-6 Luna / high.
Ana runtime kaynak temeli `b723f9b`, sürüm 0.32.0; sonraki ana commit yalnız
belge/kanıt kaydını güncelledi. Aşağıdaki 0.33 kaynakları
`data/runtime/parallel-m0/integration-033` içinde; ana runtime'a alınmadı.
Kabul özeti **10 geçti / 8 kısmi / 4 açık** olarak korunur.

## İlk 0.33 kaynak ve kalite kaydı

- Özel HEAD: `f2b9100ba2dcdb846ffafe0264bd7d1fcbacf8a1`.
- 0024 baseline işlemi ve 0025 değişmez yürütme sözleşmesi / generation 1
  sahipliği birlikte incelendi. Baseline dispatch provider oluşturmadan çalışır.
- Durdurma closure yolu ile aktif çalışma sahipliği ayrı denetlenir. Boş ve
  henüz sahiplenilmemiş baseline için dar rapor yolu vardır.
- Gerçek PostgreSQL denemesi ortak trigger içinde yanlış tabloya ait `NEW`
  alanlarının okunabildiğini gösterdi. 0024/0025 kontrolleri tabloya özel iç
  dallara taşındı; Astra bu dar düzeltmeyi inceledi.
- Bu sürümün kalite kapısı: session **52910**, chunk **aee700**, exit **0**;
  yedi komut başarılı, **581 passed / 7 skipped / 23 deselected**, strict mypy
  104 dosya, Pylint 9,32. Ayrıntılı son komut sonucu özel ağaçtaki
  `integration-033-gate-e8bf630f93c9-commands.json` dosyasındadır.
- Bu sürümün imajı: `sha256:8285dcadfd9b192b8d66d7e05039bee6a1c67940e32c187516a2f28d3069bef6`.
  İmaj kontrolü session **41532**, chunk **7e273d**, exit **0**; 113 runtime
  dosyasında kaynak ile byte eşliği. Deneme başlamadan kapı, imaj ve 119
  runtime girdisi yeniden hash ile bağlanır.

## Gerçek PostgreSQL denemeleri

| Deneme | Sonuç | Sağladığı kanıt ve sınır |
| --- | --- | --- |
| R1, session 56688 | exit 1 | Migration ve API başladı; geçerli SQL kayıt işlemi ortak trigger alan hatasında durdu. Baseline skor yok. Kendi kaynakları temizlendi. |
| R2, session 81064 | exit 1 | SQL owner alt kümesi geçti. İlk claim, wall-budget tamper, eksik/NULL/yanlış owner bağlamı ve başka run'a yazma retleri ölçüldü. Test aracının yetkisiz holdout sayımı yüzünden production dispatch başlamadı; skor yok. |
| R3, session 37747 | exit 1 | 36 ölçüm artefaktı, üç scored experiment/trajectory çifti ve kalibrasyon blobu korundu. Director kapanış yolunda ProgrammingError verdi. Nihai rapor ve ayrı queued-stop doğrulanmadı. |

R1/R2/R3 başarısızlıkları düzeltilmiş başarı kaydıyla değiştirilmez:

- `review-evidence/baseline-ownership-r1-execution-binding.json`
- `review-evidence/baseline-ownership-r2-execution-binding.json`
- `review-evidence/baseline-ownership-r3-execution-binding.json`
- `review-evidence/baseline-ownership-r3-artifact-audit.json`
- `review-evidence/baseline-ownership-proof-033-e9e5fff5d2a7.json`
- `review-evidence/baseline-ownership-proof-033-9fc429897d0c.json`

R2'de API reaped, tüm sahipli süreç/cgroup/konteynerler boş olmasına rağmen
test aracı terminal run durumu beklediği için geçici PG'yi sakladı. Sonraki
ayrı kaynak temizliği **session 33805 / chunk 010bb4 / exit 0**: yedi birim ve
cgroup, API/outer PID yokluğu, sıfır iş/skor, exact PG kimliği/etiketi/imajı ve
DB başlangıcı yeniden doğrulandı. Özel salt okunur DB kaydı korundu; yalnız
bu PG ve erişim dosyaları kaldırıldı. Diğer konteyner envanteri değişmedi.
`review-evidence/baseline-ownership-r2-resource-cleanup.json` bu işlemi
belgeler. Üç `stop_requested` satırı terminal rapor kabulü sayılmaz.

## R3 kapanış hatası ve açık kapsam

R3 test aracı hash'i
`b129717591cd88a2d10291b0ab7f8b3e662d45af2411f8496aeb535f87adae1d`.
Holdout yokluk sayımı yalnız disposable migrator ile salt okunur gözlem olarak
etiketlenir; ürün rol yetkileri genişletilmez. SQL companion hash'i
`bb3ceb119161b3e6cd915165ffbc26d97533477d4a6145b24ea46e08711f2974`.
Astra kaynak incelemesi bu sınırlı denemeye hazır buldu. R3 **session 37747 /
chunk 5f1d52 / exit 1**, 8 dakika 41 saniyede sonlandı. Tüm sahipli kaynak ve
erişim dosyaları temizlendi. Sonradan salt okunur artefakt incelemesi
**chunk b6008c / exit 0**: 36 benzersiz hücre, 36 ayrı Scorer birimi, tüm
Scorer exit kodları 0 ve dosya hash eşliği. Bu kayıtlar son DB matris/rapor
doğrulaması yerine geçmez.

Kaynak incelemesi, sıfırlanmış `reserved_wall_seconds` değerinin JSON `0.0`
olarak yazılırken SQL’de metin `0` ile karşılaştırıldığını buldu. Bu kesin bir
guard hatasıdır; R3’ün tam SQL hata metni saklanmadığı için o koşunun kök nedeni
olarak henüz kanıtlanmadı. Aşağıdaki R4/R5 PostgreSQL kontrolleri bu guard
kusurunu ve düzeltmesini bağımsız olarak doğruladı.

Hedef: gerçek CLI/API üzerinden 4 sentetik görev × 3 algoritma × 3 tohum =
36 ayrı Scorer skoru ve ayrı queued-stop raporu. Aktif sandbox sırasında stop,
checkpoint aralığında kesinti, tam restart/takeover, holdout generation zinciri,
public veri araştırması, yerel model ve AOS birlikte çalışma kabulü açıktır.
0.34 holdout değişiklikleri başka bir özel çalışma kopyasında sürer.

## Sayısal sıfır düzeltmesi — özel 0.33.1

**Önce:** R4 doğrudan PostgreSQL tanısı, session **4381 / chunk 421ace / exit 0**.
Geçerli, sahiplenilmiş ve planı henüz mühürlenmemiş aynı koşulda integer `0`
bütçe kontrolünü geçip beklenen plan-mühür hatasına ulaştı; float `0.0`
bütçe hatası verdi. Hiçbir kayıt değişmedi. Bu, SQL guard kusurunun bağımsız
kanıtıdır; R3'te saklanmamış hata metnini geri getirmez.

**Düzeltme:** `proposal_count`, `model_tokens`, `reserved_wall_seconds` ve
`reserved_model_tokens` JSONB sayısal sıfır ile karşılaştırılır. Özel HEAD
`800e5ff27d50577933aea447a8c6d1fc3bf5187a`, runtime **0.33.1**.
Yeni imaj `sha256:5e2d6e23cc9e5fdd3d6fae60789e31fd13dc882caa930d125a7d4828f1b11174`
113 dosyada byte eşliğine sahip (session 87214 / exit 0). Son özel kalite
kapısı session **43521 / chunk 587e48 / exit 0**: 581 passed, 7 skipped,
23 deselected, strict mypy 104 dosya ve yedi komutun tamamı başarılı.

**Sonra:** R5 doğrudan PostgreSQL kontrolü, session **30390 / chunk e36911 /
exit 0**, 33 vaka. Sayısal `0`/`0.0` geçer; dört sayacın eksik, null, metin,
boolean, dizi, nesne ve sıfır olmayan biçimleri kesin hata kodu/mesajıyla
reddedilir. Her vakada önce/sonra kayıt eşliği doğrulandı. Her iki denemenin
kendi API/PG/erişim dosyaları temizlendi. İş, skor veya nihai rapor üretilmedi.
Tam baseline/queued-stop rapor kabulü hâlâ açıktır.

- `review-evidence/baseline-numeric-guard-before-execution-binding.json`
- `review-evidence/baseline-numeric-guard-after-execution-binding.json`
- `review-evidence/baseline-ownership-sql-diagnostic-033-d0d6aa88d3eb.json`
- `review-evidence/baseline-ownership-sql-diagnostic-033-672cd98284df.json`

## Rapor sırası düzeltmesi — özel 0.33.2

Üretim çalıştırıcısı deneyleri `ecod_train_frozen`, `iforest`, `robust_z`
sırasıyla kaydediyor; PostgreSQL terminal belge listesi de deney sırasını
koruyor. Tamamlanan rapor doğrulayıcısının farklı bir sıra istemesi gerçek
üretim yolunu reddediyordu. Üç kayıt ve tam algoritma kümesi zorunlu kalırken
kayıtların sıra şartı kaldırıldı. Mevcut algoritma başlığı ve saklanmış
veriler değişmedi. Hedefli test gerçek `BASELINE_NAMES` sırasını kabul etti,
eksik ve yinelenen kayıtları reddetti.

- Özel HEAD `c6fc212f69b18893ade15447aaa373ef6c345ddb`, sürüm **0.33.2**.
- İmaj `sha256:359759c90fc69e7b2c76d628e4b05f599ef28d028c524ea8102cbbdf2ad3cb73`:
  session **60052 / chunk 2460df / exit 0**, 113 runtime dosyasında byte eşliği.
- Kalite kapısı **session 20480 / chunk 967a78 / exit 0**: yedi komut,
  **581 passed / 7 skipped / 23 deselected**, strict mypy 104 dosya.
- R6 test aracı deney kabulünü, SQL kontrollerini, tamamlanan matrisi ve
  queued-stop sonucunu ayrı hash bağlı kayıtlara yazıyor. Üç test kaynak
  dosyasının tam byte kopyaları saklanıyor. Hata tanısı özel PG logu ve
  açıkça etiketlenen salt okunur migrator gözleminden oluşuyor; bu gözlem
  API kapandıktan sonraki temizlik durumudur, hatalı işlemin anlık görüntüsü
  değildir.

**R6 geçti:** session **56324 / chunk ad5ce8 / exit 0**, 8 dakika 51,960 saniye.
Üretim CLI/API → Director → bağımsız Scorer yolunda **36 benzersiz skor**
(3 algoritma × 4 sentetik görev × 3 tohum), generation 1 yürütme kaydı,
kalibrasyonu tamamlanmış nihai rapor ve sıfır model kullanımı doğrulandı.
Yetkisiz istek 401, aynı anahtarla tekrar aynı run, değiştirilmiş istek 409
sonucu verdi. Ayrı queued-stop deneyi `stopped` durumuna geçti; görev, iş ve
skor sayıları sıfır kaldı.

Dört ara kanıt dosyası ile üç test kaynak kopyasının hash'leri sonradan da
doğrulandı. API reaped, sahipli süreç/cgroup/konteynerler boş, geçici PG ve
erişim dosyaları kaldırılmış; diğer konteyner envanteri değişmemiştir.
Tam rapor JSON'u koşu sırasında CLI/API doğrulamasından geçti; PG silindikten
sonra yalnız hash'i ve matris metaverisi kaldı. Raporun tamamının kalıcı kopyası
bu denemede saklanmadı. Bu sınır sonraki test aracında giderilecek; eski rapor
yeniden üretilmiş gibi sunulmaz.

- `review-evidence/baseline-ownership-r6-execution-binding.json`
- `review-evidence/baseline-ownership-proof-033-432eef866da3.json`

Bu sonuç aktif worker stop, checkpoint arası kesinti, nesil devri/restart,
holdout, public veri, model, eğitim veya AOS kabulünü kapatmaz. Ana runtime
0.32.0 ve genel kabul sayıları korunur.

## Ayrı aktif sandbox stop denemesi

Astra/high kaynak incelemesi sonrasında yalnız aktif stop senaryosu bir kez
çalıştırıldı; 36 hücrelik matris ve SQL değişiklik testleri tekrarlanmadı.
Kaynak c6fc212 / özel sürüm 0.33.2, R6 kalite ve imaj bağları korundu.
**Session 6844 / başlangıç chunk ad192a / sonuç chunk 0d6361 / exit 1**,
10,916 saniye, bildirilen bellek tepesi 487,9M, swap 0.

Alt senaryo `passed`: generation 1 Director'a ait çalışan FIT konteyneri,
aday hash'i ve seed yeniden doğrulandı; authenticated API stop sonrasında
typed rapor `stopped` döndü. Bu gözlem aktif Scorer + konteyner iddiası değildir;
stop öncesi Scorer işi yoktu. API, kalıcı DB ve saklanan 4920 byte canonical
rapor aynı SHA-256 değerini verdi:
`e3d773d9b4e70006b166350adee05f453ba0e5c7646b56e61eedce7fe703de04`.
Rapor özel koşu dizininde korunuyor; hash ve canonical byte biçimi koşudan
sonra yeniden doğrulandı. Sahipli süreç/cgroup/konteyner drain, API reaping,
geçici PG ve erişim dosyalarının temizliği geçti. Kaynak kopyaları değişmedi.

**Dış test geçmedi.** Diğer konteyner ID'leri aynı kaldı; servis envanterindeki
tek fark `rclone-gdrive-sync.service` alt durumunun `start` → `auto-restart`
olmasıydı. Bu gözlem servis geçişinin nedenini kanıtlamaz. Katı envanter eşitlik
kapısı nedeniyle gerçek exit 1 ve `acceptance_claim=false` korunur. Bu sonuç
başarılı tam test olarak yeniden etiketlenmedi; otomatik tekrar yapılmadı.

- `review-evidence/active-stop-proof-033-eda013df36d7.json`
- `review-evidence/active-stop033-r1-execution-binding.json`

Ana runtime 0.32.0; genel kabul **10 geçti / 8 kısmi / 4 açık**. Nesil devri,
tam restart/holdout, public veri araştırması ve gerçek model/AOS kabulleri açık.

## 0.34 holdout incelemesinin sınırı

Hedefli 93 CPU testi geçen aday henüz tam kapanış onayı almadı. Astra'nın
bağımlılık incelemesi; intent öncesi bütçe rezervasyonu, SQL intent kaydından
önceki kesinti, süresi dolmuş mevcut rezervasyonun recovery yolu ve
application/state/marker tekrarlarının hâlâ eksik olduğunu belirledi.
İlk tutarlı dilim aynı sahiplik neslindeki başarısız/bitless run-end kapanışı:
geri ödenmeyen bütçe, önceki onaylı durum ve nihai application marker birlikte
tamamlanmalı. Süre sonrası pozitif holdout biti uygulama, nesil devri ve tam
araştırma finalizasyonu bu dilimin kanıtı olarak sunulmayacak.

## Ana kayıt kalite kapısı

R1–R3 belgeleri ve kanıtları için ana kopyada **session 98769 / chunk 714256 /
exit 0**: yedi komut, **539 passed / 7 skipped / 23 deselected**, strict mypy
99 dosya. `review-evidence/baseline-integration-main-gate-binding.json` ile
komut kayıtları ve çıktı hash'i bağlıdır. Ana runtime dosyaları değişmedi.

R4/R5/R6 güncellemesinin son ana kalite kapısı **session 71346 / chunk 53ba56 /
exit 0**: yedi komut başarılı, **539 passed / 7 skipped / 23 deselected**,
strict mypy 99 dosya. Ana runtime dosyalarında diff yok.
`review-evidence/baseline-r6-main-gate-binding.json`, komut sonuçları ve ham
çıktı hash'ini bağlar. Kabul toplamı **10 geçti / 8 kısmi / 4 açık** kalır.
