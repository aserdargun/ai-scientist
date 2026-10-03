# Aktif primary proposal için stop adayı

> Tarihsel SQL0037 aday notu. Yeni SQL0038–0040 v2 yolunda gerçek CPU
> inflight stop, terminal rapor ve fiziksel cleanup [R9](73-native-r9-terminal-stop-proof.md)
> koşusunda geçti. Aşağıdaki ilk adayın “henüz çalıştırılmadı” maddeleri
> o tarihteki durumu anlatır; genel GPU/AOS kabulü hâlâ açık.

2026-09-30. Bu kaynak adayı ana servise alınmadı. M0 kabul toplamı değişmedi.
[Kaynak ve komut kanıtları](review-evidence/attempted-proposal-stop-candidate.json).

## Uygulanan

- Automatic stop gerçek proposal varlığından uygun recovery yolunu seçer.
  Eski SQL0031 zero-admission guard'ı korunur; aktif primary için ayrı
  `attempted-proposal-stop.v1` zarfı ve SQL0037 kullanılır.
- Zarf özgün proposal checkpoint'ini, tüm admitted job satırlarını ve
  owner generation/invocation/execution hash'ini bağlar. Tekrar denemesi
  değişmiş satırlardan yeni admission snapshot'ı üretmez.
- Süre sınırı özgün execution deadline, ilk stop+120 saniye ve özgün
  closure+120 saniyenin en erkenidir. Yeni cleanup penceresi açılmaz.
- Director ölümü, sandbox drain ve her canonical Scorer invocation/cgroup
  temizliği mevcut bağımsız Scorer tarafından doğrulanmadan kapanış olmaz.
  Normal worker failure ile recovery drain farklı native kanıtlara bağlıdır.
  SQL0036 exact-row producer'ı değiştirilmez; yeni yardımcılar özel kalır.
- Terminal inventory hash'i ile abandoned experiment/trajectory kaydı
  oluşturulur. Partial ölçümler native ledger'da korunur; kalite kararı,
  suite score veya ölçülmemiş süre üretilmez. Budget kayıtları korunur.

## Geçen doğrulamalar

Root birleşik focused CPU koşusu: 69 passed/exit0. Son genel kalite kapısı:
1360 passed, 7 skipped, 120 deselected; yedi komut ve gerçek waiter exit0.
İlk kapıda üç strict typing hatası vardı; başarısız artefakt korunur.
Kaynaklar gate boyunca değişmedi; CPU/RAM limitleri 1CPU/2GiB, swap0.

Yeni ve ayrı PG55546 üzerinde normal dört rol ile migration0037 gerçekten
uygulandı. 13 native kontrol geçti: boş run/job/authority tabloları,
role sınırları, yeni private yardımcıların EXECUTE reddi, korunmuş public
RPC ACL'leri, SQL0031 guard ve SQL0036 producer byte eşliği. Recovery intent
olmadan Director çağrısı P0001; yanlış roller 42501 ile reddedildi.
Bu kontroller admitted deney üzerinde owner/generation yarışı değildir.

İlk bootstrap wrapper'ı zaten var olan boş fixture klasörü nedeniyle SQL'den
önce durdu. İlk ACL gözlemcisi iki eski Planner RPC'sini yeni helper gibi
seçtiği için başarısız oldu; gözlemci kapsamı düzeltildi, ACL değiştirilmedi.
İki hata da korunur. Yalnız yeni boş PG container'ı durduruldu; verileri ve
eski fixture'lar korundu. Ana API/Director kimlikleri değişmedi.

## Kalan kabul

Frozen calibration gerçekten üretilmiş G1 → exact crash → normal G2 resume
→ gecikmiş G1 reddi → gerçek primary Scorer claim → typed stop → fiziksel
drain → hash doğrulanmış terminal rapor akışı henüz çalıştırılmadı. Eski
G1/G2 dar fence kanıtı bu yeni uçtan uca testin yerine geçmez.

Bu aday tek proposal/primary seed0 admitted şekli içindir. Genuine cancelled
outcome, confirmation, holdout ve birden fazla proposal için kapanış
henüz yok; desteklenmeyen şekiller pending kalır. Genel güvenli stop kabulü
tamamlanmış değildir. Ana shared-drain successor politikası devrede değildir.

## AOS oturumuna aktarılacak özet

Scientist tarafında HTTP stop ACK yalnız istek alındığını belirtir; terminal
rapor, bağımsız worker drain veya GPU release kanıtı değildir. AOS idle/quiesce
aynı amaçla kullanılamaz. `aos-scientist-runtime.v1 / wire1` hâlâ ortak runtime
admission'ı tamamlanmamış sözleşme önerisidir. Actual checkout ve capability /
version / status / cancellation / drain / release / timeout / reconciliation
yüzeyleri birlikte doğrulanmalıdır. Yeni GPU tahsis otoritesi eklenmedi.

Actual AOS HEAD ve yerel fark hash'leri kanıt JSON'undadır. AOS salt okunur
incelendi. Tek GPU kabul yürütücüsü Scientist oturumu olarak kalır. Scheduler
rezervasyonu doğrulanmadı; model/backend çalıştırılmadı, quantization/context
uygulanmadı, VRAM tepe ve GPU devir gecikmesi ölçülmedi. GPU release veya
tamamlanmış araştırma iddiası yoktur. Lisans ve genel CI ayrı işlerdir.
