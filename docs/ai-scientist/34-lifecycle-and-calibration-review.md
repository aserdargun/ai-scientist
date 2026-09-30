# Holdout süreç testi ve Farm B kalibrasyon hazırlığı

Tarih: 2026-09-27. Thinker ve bağımsız inceleme **GPT-6 Astra / high**;
uygulama worker'ları **GPT-6 Luna / high**. Ana runtime `7ba1a1b` / 0.30.0
üzerindedir. İki worker ayrı çalışma ağaçlarında ilerler; ana DB ve canlı AOS
değiştirilmez. M0 kabul sayıları **10 geçti / 8 kısmi / 4 açık** kalır.

## Süreç testi hazırlığı

Yeni stop sürücüsü, özel kaynak kopyasında çalışan gerçek Scorer worker ve
Docker konteynerinin kimliğini doğrulayıp authenticated stop/recovery yolunu
çağırmak üzere hazırlanıyor. PostgreSQL ve tüm test süreçleri ayrı ve kaynak
sınırlı olacak. Bu hazırlık henüz gerçek lifecycle kabulü değildir.

İlk stop sürücüsünün altı CPU preflight testi ana kaynaklara alındı. Tam
kalite kapısında **session 92899 / exit 0**, yedi komut başarılı:
**493 passed / 7 skipped / 22 deselected**, strict mypy 94 kaynak,
Pylint 9.37. Bağ:
`review-evidence/holdout-030-lifecycle-gate-first-pass-binding.json`.
Bu sonuç sürücü `3330d618…` ve test `9de65ddc…` içindir; daha sonraki
`lab.alembic_version` düzeltmesini veya başlatıcıyı kapsamaz.

Önceki deneme **session 14566 / exit 1** olarak korunur. Root'un seçtiği
uzun pytest geçici yolu dört mevcut Unix soket testinde adres uzunluğu
sınırını aştı. Kısa, özel bir `/home` geçici diziniyle aynı kaynaklar geçti;
üretim kodu bu hata nedeniyle değiştirilmedi. Kanıt:
`review-evidence/holdout-030-lifecycle-gate-path-failure-binding.json`.

Astra'nın başlatıcı `987733bf…` incelemesi dört düzeltme istedi: temizlik
süresi rezervinin komutlara aktarılması, başarısız Docker sorgusunun yokluk
kanıtı sayılmaması, recovery sonrası pytest çocuğunun yeniden denetlenip
toplanması ve ilk üç hazırlık worker'ının sahiplik/deadline kaydı. Luna
düzeltmeleri sürdürüyor. İnceleme:
`review-evidence/holdout-030-lifecycle-r4-launcher-review.json`.

R5 başlatıcısında bu düzeltmelerin ilk bölümü ve altı ek CPU güvenlik testi
vardır; worker'ın hedefli kontrolü **12 passed / exit 0** (session 52489).
Astra iki ek hata belirledi: erken hazırlık hatasında orijinal testin sildiği
ledger satırlarına temizlik yolunun bağımlı kalması ve SIGSTOP ile durmuş
pytest çocuğunun tuttuğu lifecycle kilidinin dış temizliği kilitlemesi.
Düzeltmeler sürer; gerçek süreç testi başlamadı. Kanıt:
`review-evidence/holdout-030-lifecycle-r5-launcher-review.json`.

R6 (`544ebe7f…`) bu iki bulguyu düzeltti ve Astra tarafından izole çalıştırma
için uygun bulundu. Gerçek ilk deneme **session 30803 / exit 1**, 2,314 sn
sonra hazırlıkta durdu; rezervasyon/worker-ready kaydı oluşmadı. Başlatıcı
zirvesi 73,4 MiB, swap 0; bu değer ayrı PG cgroup'unu kapsamaz. Kendi DB'sinin
kaldırıldığı ve erişim bilgilerinin silindiği doğrulandı. 174 snapshot dosyası
ana kaynaklarla eşleşti. Kayıt yalnız `RuntimeError` türünü tuttuğundan bir
sonraki denemeden önce güvenli aşama/hata tanısı ekleniyor. Kanıt:
`review-evidence/holdout-030-lifecycle-r6-first-execution.json`.

Aşama kaydı eklenmiş sonraki deneme **session 99634 / exit 1**, 2,400 sn:
migration exit 0, ardından DB adres kimliği kontrolünde ret. SQL'deki
`inet_server_addr()::text` ağ maskesini korurken Docker yalnız adres döndürür;
başlatıcı ve stop sürücüsü `host(inet_server_addr())` ile aynı biçimi
kullanacak şekilde düzeltiliyor. [PostgreSQL 16 sözleşmesi](https://www.postgresql.org/docs/16/functions-net.html).
DB/erişim bilgileri yine temizlendi; worker başlamadı. Kanıt
`review-evidence/holdout-030-lifecycle-r7-endpoint-failure.json`.

Adres biçimi düzeltildikten sonraki deneme **session 89531 / exit 1**,
11,053 sn sürdü. Migration geçti ve üç gerçek hazırlık Scorer birimi başladı;
bu birimler önceden hazırlanmış sentetik skorları kullandı. Pytest çocuğu
holdout rezervasyonuna ulaşmadan kapandı. Zaten kapanmış çocuğun çıktısı
toplanmadığı için hatanın kesin nedeni bu denemeden çıkarılamıyor; sonraki
denemeden önce özel, sınırlı ve parola redaksiyonlu stdout/stderr kaydı
ekleniyor. DB, erişim bilgileri ve sahip olunan worker'lar temizlendi.
Başlatıcının 196,6 MiB zirvesi kardeş PG/Scorer cgroup'larını kapsamaz.
Kanıt: `review-evidence/holdout-030-lifecycle-r8-child-failure.json`.

Hata kaydı düzeltildi; Astra kesilmiş parola parçaları için ek sınır kontrolünü
inceledi. Sonraki **session 6071 / exit 1**, 12,357 sn denemesi artık pytest
çıkışını ve temizlenmiş traceback'i saklıyor: üç hazırlık worker'ı exit 0
döndürse de `completed` sonucu doğrulanamadı. Snapshot üzerinde bağımsız
`compute_harness_hash` çağrısı `Dockerfile.sandbox` eksikliğini gösterdi;
`docker/sandbox/requirements.txt` de kopyalanmamıştı. Scorer bu tür dosya
hatasını yeniden denenecek altyapı hatası olarak işler; o denemedeki tekil
worker durumları saklanmadığından bu neden bağlantısı kaynak çıkarımıdır.
Kopyalama ve DB öncesi fingerprint eşliği kontrolü düzeltiliyor. Temizlik
yine başarılı; holdout rezervasyonu oluşmadı. Kanıt:
`review-evidence/holdout-030-lifecycle-r9-snapshot-failure.json`.

## Gerçek stop/recovery süreç kanıtı

Snapshot düzeltmesi ve DB öncesi fingerprint kapısı Astra incelemesinden
geçti. **Session 4504 / exit 0**, 16,523 sn denemesinde üç hazırlık Scorer
işi `completed` döndürdü; ardından gerçek holdout worker'ı ve çalışan fit
konteyneri PID/start/boot/unit/InvocationID/cgroup kimlikleriyle doğrulandı.
Yetkili FastAPI stop çağrısı ayrı production recovery servisini başlattı.
Worker ve konteyner kapandı, özel marker kaldırıldı, holdout rezervasyonu
`failed / worker_recovered` oldu; sonuç biti ve sonuç hash'i boş kaldı.
Deney, skor, iş, sonuç, onay ve kota sayıları değişmedi.

83 dosyalık harness fingerprint'i ana 0.30 sürümüyle aynı; 177 snapshot
dosyası çalıştırma sonunda ana kaynaklarla eşleşti. İmaj
`sha256:2ad07bb30f69a9a97ed05acbc402ebafacd06566209d0e70819432c62540484e`.
Başlatıcı zirvesi 379,6 MiB, swap 0; PG, Scorer ve sandbox kardeş
cgroup'ları bu ölçüme dahil değildir. Özel PG ve erişim bilgileri temizlendi;
mevcut AOS/WinBoat konteynerleri korundu. Kanıtlar:
`review-evidence/holdout-030-lifecycle-r10-live.json` ve
`review-evidence/holdout-030-lifecycle-r10-execution-binding.json`.

Bu sonuç çalışan süreç/konteynerin gerçek durdurulmasını kanıtlar. Baseline
skorları ve normalizasyon sentetiktir; API çağrısı süreç içi TestClient
üzerindedir. Fixture, yeniden kullandığı testi `FixturePaused` ile durdurduğu
için orijinal testin holdout sonrası bütün assertion'ları çalışmış sayılmaz.
Kaynak ve son durum doğrulaması, drain'in terminal SQL CAS'tan önce olduğunu
bağımsız olarak kanıtlamaz; bu sıra için ayrı gözlem gerekir. Başarılı Farm B
skoru, 10 KEEP, canary ve model/AOS araştırma kabulü açık kalır.

## Bu dilimin tam kalite kapısı

Entegre review sürücüleri ve testler için **session 80368 / exit 0**:
**507 passed / 7 skipped / 22 deselected**, strict mypy 94 kaynak ve
yedi komutun tamamı başarılı. Süre 55,102 sn, bellek zirvesi 480,5 MiB,
swap 0. Kısa ve özel pytest geçici yolu kullanıldı. Ana runtime'ın 105
kaynak hash'i önceki imaj bağıyla eşleşiyor; sürüm 0.30.0 kalır. Kanıt:
`review-evidence/holdout-030-lifecycle-quality-gate.json` ve
`review-evidence/holdout-030-lifecycle-quality-gate-binding.json`.

Testin baseline ledger ve normalizasyon girdileri sentetiktir. Stop yolu
FastAPI TestClient ile aynı süreçte çağrılır. Worker'ın kapanması ile terminal
SQL güncellemesinin sırası ayrıca kanıtlanmadıkça bu sıra doğrulanmış sayılmaz.

## Farm B kalibrasyon kolu

`calibration-031` çalışma ağacının ilk altı dosyalık dilimi hash doğrulamasıyla
`data/runtime/parallel-m0/delivery/calibration-031-r1-review` altında donduruldu
ve Astra tarafından incelendi. Manifest, tek Scorer ölçümü, değişmez özel
kayıtlar, reducer ve ölçülmüş holdout süitine DB bağlantısı taslağı vardır.
Worker altı hedefli test, Ruff, strict mypy ve derleme için exit 0 bildirdi;
migration henüz gerçek PostgreSQL'e uygulanmadı.

Astra entegrasyon öncesi altı hata bildirdi: metriklere yanlış semantik veri
biçimi verilmesi, migration'ın definer sahibinden gerekli yetkileri kaldırması,
dondurulmuş işe sonradan görev eklenebilmesi, süresi geçen tamamlanmış kaydın
aynı içerikle tekrar okunması yerine INSERT denenmesi, ölçüm öncesinde eksik
görev kimliği kontrolü ve alarm eşik sırası doğrulamasının eksikliği. Luna
bunları düzeltmektedir. Kaynak bağı ve gereken testler:
`review-evidence/care-calibration-031-r1-source-review.json`.

Bu altı bulgu için hazırlanan R2 kaynakları ayrıca hash doğrulamasıyla
`data/runtime/parallel-m0/delivery/calibration-031-r2-review` altında saklandı.
Worker **8 passed / exit 0** bildirdi; iki yeni vaka gerçek PDM/NRM metrik
fonksiyonlarını fixture sandbox çıktısıyla çağırır. Bu, Docker ölçümü değildir.
Astra R2 değişikliklerinde altı ilk bulgunun düzeltildiğini gördü. Yeni
`SELECT ... FOR UPDATE` sorgusunun Scorer rolünde gerekli kilit yetkisi eksik;
bu düzeltilince ayrı PostgreSQL'de saklama, dondurma, eşzamanlılık, yeniden
okuma ve rol sınırları doğrulanacak. Kaynak incelemesi:
`review-evidence/care-calibration-031-r2-source-review.json`.

R3'te yalnız Scorer'a açılan, sabit search_path ve session_user kontrollü
dar DB kilit fonksiyonu eklendi. Astra bu saklama dilimini ayrı PostgreSQL
testine uygun buldu; geniş UPDATE yetkisi verilmedi. Yeni test 135 sentetik
receipt ile saklama/dondurma/yeniden okuma yolunu sınayacak. Görev satırı
kaydı, tutarlı PDM fixture semantiği ve yarış hatasının kesin nedeni için
test genişletiliyor. `review-evidence/care-calibration-031-r3-source-review.json`.

Genişletilmiş saklama testi ayrı PostgreSQL 16 üzerinde **session 20829 /
exit 0** ile geçti: **1 passed / 24,93 sn**; toplam başlatıcı süresi 28,080 sn.
Migration `0022_care_baseline_calibration` uygulandı. Migrator, Director,
Planner ve Scorer bağlantılarının aynı özel DB örneğine ve beklenen rollere
ait olduğu doğrulandı. 15 sentetik görev ve 135 sentetik receipt ile
saklama, değişmez dondurma, dondurmaya karşı görev ekleme yarışı,
önceden belirtilen son süreden sonra aynı kaydın yeniden okunması,
çelişen kayıtların reddi ve Director/Planner erişim retleri geçti.
Ölçülmüş süit kaydının başlığı ve temsili görev satırı gerçek DB trigger'larıyla
sınandı. Bu test 15 gerçek görevin tam kurulumunu veya aday ölçümünü kapsamaz.

Başlatıcı bellek zirvesi 183,3 MiB, swap 0; ayrı PG cgroup'u bu zirveye dahil
değildir. Snapshot ve kaynak hash'leri çalıştırma boyunca eşleşti; özel DB
konteyneri ve erişim bilgileri kaldırıldı. Kaynak ve çıktı kanıtı
`review-evidence/care-calibration-031-pg-91e1c7e3ecb5.json`; gerçek süreç bağı
`review-evidence/care-calibration-031-pg-execution-binding.json`.
Bu **SQL saklama kanıtıdır**; 135 gerçek Farm B ölçümü değildir.

Kalıcı hücre sahipliği ve nesil kontrolü, çökmede korunacak süre rezervleri,
worker yaşam döngüsü ve **15 görev × 3 algoritma × 3 seed = 135 ölçüm**
çizelgelemesi devam eder. Bu dilim ana runtime'a alınmadı; 0.30 imajında
bulunmaz. Gerçek Farm B baseline ölçümü, tamamlanmış kalibrasyon veya holdout
skoru olarak sunulamaz. Kaynak/kayıt doğrulaması için önceki
`33-care-farm-b-source-review.md` geçerlidir.

## Kalan kabul sınırları

Gerçek stop/recovery süreç dilimi geçti; drain/CAS sırasının bağımsız kanıtı,
ölçülmüş Farm B kalibrasyonu, 10 KEEP kontrolü, canary, gerçek Qwen araştırması
ve AOS ile birlikte çalışma açıktır.
GPU/AOS koordinasyon yanıtı beklenirken CPU geliştirmesi ilerler. Remote yok;
yerel geliştirme ve kalite kapısı, açılmış PR yerine geçmez.
