# AOS ve Scientist kabul koşusunun süre planı

2026-10-02. Bu belge yeni GPU kabulünün sonucu değildir. Önceki gerçek
ölçümler, kaynak incelemesi ve sıradaki iki ayrı koşunun sınırlarını kaydeder.

## Neden önceki plan yeterli değildi?

Native launch, doğrulama belgesini 300 saniye için üretiyordu. Yeni bir AOS
görevi için operatör en az 210 saniye kalan süre istiyor; ikinci görev bu
yüzden ilk 90 saniyede başlamalıydı. Oysa aynı sentetik snapshot'ın önceki
gerçek koşularında dokuz CPU baseline ölçümü **299,582** ve **298,633 saniye**
sürdü. CPU ölçümleri sırasında başlayan AOS görevi, GPU için beklediğini
kanıtlamıyordu.

Önceki normal Scientist model çağrısının yüklenmesi 77,199; çıkarımı 8,760;
boşaltılması 0,721 saniyeydi. Bunlar yeni koşu için süre garantisi değildir.
AOS'un karar çağrısı, retained doğrulama dahil, özgün 120 saniye sınırına
tabidir. Kuyruğa ilk model yüklemesinin başında girmek bu süreyi tüketebilir.

## Kaynak değişikliğinin kapsamı

Pinli native launch girdisi `artifact_validity_seconds` seçebilir:
**tam sayı 1–900**, varsayılan **300**. Uzun süre için mevcut pinli retained
review içindeki geçerli özgün yetki zorunludur. Belgenin sonu bu yetkinin
sonuna kırpılır; doğrulama yetkiyi tüketirse belge yayımlanmaz. Mevcut belge
değiştirilmez veya yenilenmez; yalnız gerçek yeni doğrulamadan yeni belge
üretilir. Startup, model, control, provider ve resolution süreleri uzatılmaz.

Bu kaynak değişikliği, önceki `4a71c6c` kaynak paketini yeni koşu için
geçersiz kılar. Eski paket ve ölçümler tarihsel kanıt olarak korunur;
güncel kaynak ve yapılandırma hash'leri karşı oturumla yeniden eşleştirilir.

## İki ayrı gerçek kabul koşusu

| Aşama | Gereken kanıt |
| --- | --- |
| AOS ilk görev | Gerçek model kararı, exact hello sonucu ve AOS retained resolution |
| Scientist A | Ayrı typed admission; üç baseline × üç seed; tek sınırlı yerel model önerisi ve bağımsız puanlama |
| GPU paylaşımı | Scientist A'nın aynı owner/request/fence'i aktifken AOS queued kaydı; doğal Scientist kapanışı ve fiziksel release; daha yüksek fence ile AOS edinimi ve tamamlanması |
| Araştırma raporu | AOS tarafında aynı run'ın terminal raporu, hash ve save/readback eşliği |
| Scientist B | A tamamlandıktan sonra yeni run/action/idempotency kimliğiyle ikinci ayrı deney |
| Kontrollü iptal | B'nin doğrulanmış canlı inference fazında typed stop; stop_requested; aynı generation'ın iptali; model/işçi ölümü ve GPU bırakımı; terminal rapor |
| Temizlik | Yalnız bu koşulara ait PID/start/invocation/cgroup ve GPU süreçlerinin yokluğu; canonical scheduler readback; belirsizlikte quarantine |

İkinci AOS görevi, Scientist A'nın doğrulanmış model yükleme/çıkarım
penceresinde başlatılır. Önceki 77 saniyelik yüklemede yaklaşık 30 saniye
sonrası bir gözlem noktasıdır; sabit bekleme gerçek kuyruk kanıtının yerini
almaz. Özgün 210 saniyelik belge rezervi ve 16 GiB kullanılabilir RAM
önkoşulu korunur. Bu önkoşullar sağlanmaz veya queued çakışma gözlenmezse
GPU paylaşımı maddesi geçmez.

Her Scientist deneyi tek öneri, en çok 600 saniye ve 30.000 model tokenı
ile sınırlıdır; üst API sınırı 900 saniyedir. İkinci deney ilk deneyi yeniden
açmaz. İptal/rapor çağrıları, inference belgesi sona erse bile kendi özgün
API/action/generation yetkileri geçerliyse tamamlanabilir. Süresi dolmuş
belgeyle üçüncü bir native model çağrısı başlatılamaz.

## Durum ve kanıt sınırları

- Süre düzeltmesi tamamlandı: 61 hedefli kontrol ve zorunlu tam kapıda
  **3033 test**, yedi komut **exit 0**. Bağımsız kaynak incelemesi geçti.
  Operatör üreticisinin dosya hazırlığı doğrulandı; yeni GPU kabulü başlamadı.
  [Kaynak ve kapı kanıtı](review-evidence/native-artifact-window-20261002.json).
- Önceki tam CPU bileşimi 23,916 saniyeydi; canlı service rights/SQLite/IPC
  maliyeti o fixture'da ölçülmedi. Gerçek 3/30 saniye sınırlarının geçtiği
  ayrıca gözlenmelidir.
- AOS v8 kaynak/config karşı incelemesi bekleniyor. Doğrudan oturum mesaj
  aracı bağlantı hatası veriyor; teslim edilmiş veya onaylanmış sayılmıyor.
- Tek GPU tahsis otoritesi mevcut SQLite scheduler'dır. AOS kaynağına ve
  diğer oturumun servislerine Scientist tarafından müdahale edilmez.
- Lisans, genel CI, public veri araştırması ve öğrenilmiş adaptör kabulü
  bu iki koşudan ayrı kalır.

Kaynak incelemesi ve tarihsel ölçüm girdilerinin özel kayıt SHA-256'sı:
`c444d361cad1ce6c404425e69e21e17a0e59289e62781d46d76a00fad6b82826`.
Önceki [gerçek araştırma](114-first-native-research-report.md) ve
[retained düzeltmesi](115-native-retained-resolution-wiring.md) korunur.
