# AOS ve uygulamaya özel entegrasyon: sonraki aşama

## Ürün sınırı

AI Scientist bağımsız deney servisi kalır. AOS web uygulamasından hedef ve yetkili
veri seçimini alır; uzun işi kalıcı Lab run olarak yürütür. AOS foreground slotu
araştırma boyunca tutulmaz. SWAPP'a özgü endpoint, alan eşlemesi ve şirket
politikası özel fork/adaptördedir; genel çekirdek bunları gerektirmez.

## Bugün hazır olan / açık kalan

| Dilim | Mevcut durum | Tamamlama kanıtı |
|---|---|---|
| Scientist kullanıcı akışı | Amaç/veri/yöntem/bütçe/rapor CPU akışı hazır | Saha amacı örnek raporu |
| Uygulama varlık kimliği | Kullanıcı beyanı, advisory-only | Yetkili asset resolver + erişim denetimi henüz eklenecek |
| Veri kaynağı | İzinli PostgreSQL ve snapshot yolu | Uygulamaya özgü katalog/sensör/zaman/birim eşlemesi |
| AOS kontrol adaptörü | Sürümlü API/kontrol altyapısı var | Aynı checkout/config/caller ile native bağlantı doğrulaması |
| Native dışlama | Shared-only aday ve DTO tasarımı | Gerçek producer + Scientist consumer + fiziksel kanıt |
| GPU ortak çalışma | Tek scheduler/fencing/quarantine korunur | İki tarafın ilerlediği sınırlı gerçek koşu ve iptal |
| Öğrenme | Ledger, seçili hafıza, eğitim hazırlık kayıtları | İzinli veri → öğretmen → değerlendirme → onaylı terfi |

AOS oturumu 2026-10-03 aktarımı: 41 dosya/86 giriş shared-only adayı
uygulanmadı. Native exclusion schema `aos.native-exclusion.v1` mevcut; gerçek
`read_native_exclusion` producer ve Scientist host consumer açık. Kaynak/CPU
incelemesi canlı native kabulü değildir. Aktif AOS süreçleri korunur.

## Uygulamaya özelleştirilecek sözleşme

1. **Yetki:** Uygulama kullanıcısı/tenant/asset yetkisini Lab principal ile sunucuda
   eşle. Request/idempotency ve run/task/action kimliklerini sakla. UI'dan gelen
   owner veya asset metnini yetki kanıtı olarak kabul etme.
2. **Veri:** Asset → salt okunur kaynak → sensörler/birimler → timezone/UTC →
   zaman aralığı → snapshot/hash eşle. Sampling, boşluk, eksik değer, çalışma modu,
   olay etiketleri, bakım kayıtları ve veri kullanım iznini açık tut.
3. **Görev:** Dijital ikiz veya kestirimci bakım amacı, değerlendirme metriği,
   aday aileleri ve hiperparametre aralıkları, süre/token/kaynak bütçesini dondur.
   Sonradan farklı veri veya hedefle aynı idempotency key'i kullanma.
4. **Uzun iş:** Start kısa yanıtla run ID döndürür. Poll/stop/report ayrı çağrılar;
   rapor hash ve terminal durum doğrulanmadan uygulama görevi başarılı sayılmaz.
5. **Saha sonucu:** Mod, tolerans, NN normal değer, residual/OMR ve sensör katkısını
   göster. Alarm/iş emri otomasyonunu bu deney skoruna doğrudan bağlama; uygulama
   politika ve insan doğrulaması ayrı karar verir.
6. **Geri bildirim:** İncelenen olay/yanlış alarm, zaman ve gözlemci yetkisini
   kanıt referanslarıyla kaydet. Eğitim dışa aktarımına ayrıca izin/uygunluk uygula.

## Paylaşılan donanım protokolü

Tek GPU tahsis otoritesi kullanılır. Owner, principal, generation/fencing,
acquire/cancellation/drain/release, timeout ve capability/version pinleri çağrı
başlamadan doğrulanır. CPU/API/DB izolasyonu toplam host bütçesine katılır.
16 GB VRAM'e iki büyük modelin aynı anda sığdığı varsayılmaz; çağrı dilimleri
sınırlanır ve devir fiziksel kapanışla kanıtlanır. Daha büyük host'ta eşzamanlılık
ancak ölçüm, profil ve admission politikasının kontrollü değişikliğiyle artar.

AOS idle/quiesce, GPU release kanıtı değildir. Cleanup belirsizse tahsis
quarantine'de kalır. Eski işçinin gecikmiş cevabı yeni nesli kapatamaz.
`running → stop_requested → doğrulanmış terminal` ayrımı korunur.

## Bir sonraki aşamanın bitti ölçütü

- Aynı onaylı sözleşme ve source/config/caller pinleriyle adapter bağlanır.
- Yanlış owner/eski generation, revoke/timeout/crash ve cleanup başarısızlığı
  CPU testlerinde reddedilir; retry tek işi ve aynı bütçeyi korur.
- Kullanıcı işiyle çakışmayan rezervasyonda AOS gerçek kontrollü görev yürütür;
  fiziksel GPU devri sonrası Scientist kısa öneri/deney ve bağımsız puanlama yapar.
- AOS sonucu doğrular; iki tarafta işçiler ve GPU sahipliği temiz kapanır.
  Kontrollü inflight iptal/toparlanma ayrıca gösterilir.
- Commit çifti, yerel fark hashleri, sözleşme, model/quantization/context,
  VRAM tepesi, bekleme/devir/çalışma süreleri ve cleanup receipt kaydedilir.
- Uygulama kimlik/veri eşlemeleri ve örnek yapılandırma özel sırlar olmadan belgelenir.

GPU kabulünün tek yürütücüsü Scientist oturumudur. Ayrı açık bakım penceresi ve
aktif iş kontrolü olmadan çalışan AOS değiştirilmez. Ayrıntılı teknik tarihçe:
[koordinasyon](06-aos-coordination.md), [kabul kaydı](m0-acceptance.md).
