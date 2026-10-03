# Native kalibrasyon/stop denemesi ve güncel AOS preflight

2026-09-30. Scientist kaynak temeli `ba12a0c`; AOS HEAD `ed6e857`.
Ana servisler deploy edilmedi; AOS salt okunur incelendi. M0 kabul toplamı
değişmedi. [Komut ve kaynak kanıtı](review-evidence/native-calibration-stop-and-aos-preflight.json).

## Gerçek native deneme — başarısız

Yeni PG55546 fixture'ında dört bağımsız sentetik aile, 36 normal baseline
hücresi, bir proposal ve sıfır model token bütçesi kullanıldı. Özgün wall
bütçesi 300 saniyeydi. Test kopyasına kalibrasyon sonrası ve proposal Scorer
claim sonrası gözlem hook'ları eklendi; üretim kodu değiştirilmedi. Aynı
mevcut global Director P1 kilidi kullanıldı; yeni GPU tahsis otoritesi yok.

`57bb6614-1cb1-46e2-8b59-842d6f99c732` koşusu 180 saniyelik gözlem
sınırında kalibrasyonu tamamlamadı. Root doğrulama waiter'ı exit1;
gözlemci kendi koşusuna authenticated typed stop gönderdi. G1 crash,
G2 resume, gecikmiş G1 mutation ve primary proposal claim aşamalarına
ulaşılmadı. Bu koşu controlled proposal stop kabulü değildir.

Hata yolunda test gözlemcisi mevcut launcher'ın kapanışını beklemeden
döndü. Sonraki readback: launcher PID yok, exact Director owner ölü ve
cgroup boş, mevcut Scorer işler quiescent. Launcher'ın gerçek exit kodu
yakalanmadı. Parent unit 187.679 saniye wall / 4.733 saniye CPU / 347.7 MiB
tepe gösterdi; bu yalnız gözlemci parent'ın ölçümüdür, tüm host/model ölçümü
değildir. Gözlem timeout'u deneyin o anda durduğunu kanıtlamaz.

Migrator READ ONLY son ledger: `stop_requested`, generation1 active,
reportNULL, beş committed baseline score, sıfır aktif job, calibration0,
proposal0, recovery0, closure0. Deadline daha sonra özgün haliyle doldu.
Süre/budget/row reset edilmedi. Normal terminal stop raporu oluşmadı.
Süreçlerin kapalı olması terminal ledger veya GPU release kanıtı değildir.
Yalnız kendi API fixture'ı ve fiziksel quiescence doğrulandıktan sonra kendi
PG container'ı durduruldu; bütün satırlar, dosyalar ve önceki fixture'lar korunur.

Sonraki R5 gözlemci kaynağı mevcut launcher handle'larını ilk-stop ve özgün
execution deadline içinde yeniden poll edecek şekilde taslaklandı. Yeni
request için 1800 saniyelik CPU bütçesi taslağı mevcut; eski 300 saniyelik
koşu değişmez. 36 gerçek hücre korunur. Yeni PG/wrapper/source review ve
abort davranışı doğrulanmadan bu taslak yürütülmez. Observation timeout
nedeniyle eski koşu yeniden başlatılmaz. Yeni model indirme/eğitim yok.

## AOS kaynak uyumluluğu

Actual checkout'ta artık `services/decider/broker_worker.py`,
`services/bonsai/broker_worker.py` ve ortak `TurnGate` var. Yeni async S1
caller ile SQL0020 stale/expired unsent approval düzeltmesi kaynakta görüldü.
Bu gözlem AOS migration/test/deployment kabulü değildir. Dosyalar hash'lerle
bağlandı; AOS modülleri import edilmedi ve servisleri çalıştırılmadı.

Preflight CLI açık `--source-profile` ister:

- `historical_hooks`: eski CLI flags, gpu_turn ve lab_external hook'ları.
- `runtime_v1`: yeni Scientist modülleri, worker entrypoint'leri, wire integer1,
  üç profile ID, 128KiB frame ve sabit broker unit kaynak işaretleri.

Tarihsel `--shared-gpu-turns` / `--lab-external-*` flags hâlâ actual checkout'ta
yok; yeni adapter için evrensel gereksinim değildir. Dirty/untracked kaynak,
yanlış HEAD, symlink ve hash değişimleri reddedilir. Required untracked
dosyalar hash'lenir ve açık `required_source_untracked` koduyla reddedilir.
İki profilde de boolean `True`, wire integer1 yerine geçemez.

Boolean sürüm regresyonu önce 1failed/8passed; düzeltmeden sonra genişletilmiş
15 focused CPU testi geçti. Daha açık untracked hata koduna geçişte eski
assertion güncellenene kadar genel gate başarısız oldu; bütün başarısız
komutlar korunur. Son genel gate ve actual readonly CLI sonucu kanıt JSON'undadır.

Kaynak işaretleri doğru olsa bile CLI exit3/pending ve admissionfalse verir;
uyumsuz veya bağlı olmayan checkout exit2/unsupported olur. Başarılı admission
yolu yoktur. Actual `runtime_v1` kontrolü dirty/untracked checkout nedeniyle
exit2 verdi. Bu kontrol GPU modeli veya AOS işi başlatmadı.

## AOS oturumuna aktarılacak kısa özet

Worker dosyaları artık mevcut; yeniden eski yamalı kopyayı actual checkout
gibi kullanmaya gerek yok. `aos-scientist-runtime.v1 / wire1` ortak runtime
admission'ı hâlâ tamamlanmamış öneridir. Deployment/source/principal/host
authority/capability/version bağları ve Bonsai caller tamamlanmalı; shared
status/cancel/reconcile yüzeyi yok. Yerel async cancel/timeout yalnız beklemeyi
keser; remote ticket/lease temizliği veya GPU release değildir. HTTP stop ACK,
terminal rapor ve AOS idle/quiesce da GPU release kanıtı değildir.

Scientist trusted unit/cgroup/GPU drain, quarantine ve release otoritesini
korur. GPU kabul koşusunun tek yürütücüsü bu oturumdur. Scheduler rezervasyonu
doğrulanmadı; gerçek AOS/GPU koşusu, model/quantization/context ve VRAM tepe /
GPU devir gecikmeleri ölçülmedi. Push, merge ve deploy yapılmadı. Lisans ve
genel CI bu kabulden ayrı kalır.
