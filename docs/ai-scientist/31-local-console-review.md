# Yerel kontrol arayüzü — teslim ve doğrulama

Tarih: 2026-09-26. Adres: **http://127.0.0.1:8788**.
Kullanım ve yeniden başlatma: [console/README.md](../../console/README.md).

## Kullanılabilir yüzey

- Genel bakış ve Kabul maddeleri, `m0-acceptance.md` tablosunu okuyarak
  **10 geçti / 8 kısmi / 4 açık** gösterir. Bunlar 22 kabul kapısının durumudur;
  işin tamamlanma yüzdesi veya süre tahmini değildir.
- Her maddede açıklama ve izinli kaynak kanıtı açılır; dosya SHA-256 değeri
  gösterilir. Büyük metnin kesildiği belirtilir. Kaynak dışı dosya, symlink ve
  yol kaçışı kabul edilmez; HTML kanıtları script yetkisi alamaz.
- **Kontrolü çalıştır**, sabit CPU test grubunu gerçekten çalıştırır.
  Sistem ekranında sonuç, gerçek çıkış kodu ve çıktı gösterilir.
- RAM, disk, CPU yükü ve okunabildiğinde NVIDIA GPU ölçümleri host'tan gelir.
- Deneyler ekranı mevcut Lab API'ye bağlandığında kayıtlı süitle başlatma,
  UUID ile izleme, durdurma ve hash doğrulamalı rapor okuma sağlar.

Çalışan konsolda **Lab API yapılandırılmamıştır**. Başlatma devre dışıdır ve
neden kullanıcıya gösterilir. Konsol DB, Director, model, GPU broker veya AOS
başlatmaz. Gerçek araştırma için API ve dispatcher bağlantısı gerekir; yerel
model denemeleri ayrıca bekleyen AOS koordinasyonuna bağlıdır.

## Süreç ve kaynak sınırları

`swapp-ai-scientist-console.service`, proje `.venv` ortamından loopback'te
çalışır: **512 MiB RAM / %50 CPU / 64 task / swap 0**. Bir seferde tek CPU
kontrolü ayrı serviste **1 GiB / %50 CPU / 64 task / 120 saniye / swap 0**
sınırındadır. Başlatıcı dolu porttaki başka süreci kapatmaz.

UI ve Python sunucusu ayrı `console/` dizinindedir. Araştırma runtime sürümü
0.29.0 ve 76 dosyalık harness parmak izi değişmez. Canlı AOS veya SWAPP
uygulama dosyaları değiştirilmedi. API kimlik bilgisi private sunucu dosyasında
kalır; tarayıcıya aktarılmaz. UI çalışma zamanında yalnız kendi yerel origin'ini
kullanır. Host/Origin ve özel JSON başlığı denetimleri uygulanır.

## Test kanıtı kapsamı

İlk başarılı tam proje kalite kapısı session **5756 / exit 0**: yedi komut
başarılı, **384 passed / 7 skipped / 13 deselected**, core strict mypy
84 kaynak, Pylint 9.40, wheel/import başarılı. Core Pylint/Bandit/mypy kapsamı
console'a genişletildi diye sunulmaz; console hedefli testleri ve Ruff dahildir.
Bu önceki kapı `local-console-pre-astra-quality-gate-binding.json` ve
`evidence/quality-gate-local-console-pre-astra.json` içinde tarihsel tutulur.

**Son kaynaklara bağlı kapı: session 72089 / exit 0**, yedi komut başarılı,
**389 passed / 7 skipped / 13 deselected** (9.71 saniye), mypy 84 kaynak,
Pylint 9.40. `evidence/quality-gate-local-console.json` ve
`review-evidence/local-console-quality-gate-binding.json` kaynak hash'lerini
ve gerçek çıkışı bağlar. Gate boyunca bu dosyalar değişmedi.

Gerçek tarayıcı akışı session **15406 / exit 0**, **14/14**: canlı kabul
sayıları, üç filtre, gerçek kanıt görüntüleme, Escape, yenilemede geçerli kanıt
URL'si, origin/komut reddi, bağlantısız API, CPU kontrolü, mobil taşma ve
tarayıcı hataları. `review-evidence/local-console-browser-review.json`.

Butondan gerçek CPU testi `_5S4gsdZWU6icdOwEp9Mqg`: **49 passed / exit 0**,
2.58 saniye pytest süresi. Kabul kaydı değişmedi; GPU veya araştırma koşusu
başlamadı. Ham uyarılar kayıt içinde korunur.

Deney formu testi **tarayıcıda yakalanan fixture yanıtlarıyla** yapıldı: aynı
payload tekrarında aynı idempotency key, değişen payload'da yeni key, süit
öneri tavanı, model izni kapalıyken fake-json, stop ve rapor etkileşimi.
Bu testte gerçek upstream koşu sayısı **0**; gerçek Lab API/model uçtan uca
kabulü değildir.

### Korunan başarısız denemeler

- İlk tarayıcı denemesi **60462 / exit 1**: kanıt URL'sinin ikinci kez
  encode edilmesi 404 üretti. Düzeltildi; sonraki 14/14 akış gerçek kanıtı açtı.
  `local-console-browser-review-v1-failed.json` korunur.
- İlk tam gate **35815 / exit 1**: 384 test ve mypy sonrası başlatıcının
  PATH'inde `uv` bulunmadı. PATH düzeltildi ve yedi komut yeniden başarılı
  çalıştı. `local-console-quality-gate-v1-failed.json` eski gate dosyasını
  açıkça **stale** işaretler; onu yeni başarılı kanıt saymaz. İki denemenin
  ham çıktıları ayrı `*.stdout.txt` dosyalarında korunur.
- Astra/high son incelemesi, hatalı principal ayarında token'ın hata metnine
  girebilmesini, yanlış servisin HTTP 200 ile kabul edilmesini ve
  `stop_requested` sonrası UI takibinin kesilmesini buldu. Üçü GPT-6 Luna/high
  worker'larıyla düzeltildi ve Astra/high son baytları yeniden inceledi;
  bildirilen engeller kapandı. Hatalar artık sabit güvenli metindir; sağlık
  yanıtı JSON türü, `status=ok`, `service=lab-api` ve 4096 bayt sınırıyla
  denetlenir. Stream byte 4097'de durup kapatılır; yanlış servise token
  gönderilmez. On hedefli upstream testi başarılıdır ve son tam gate'e dahildir.

### Son tarayıcı ve dağıtım doğrulaması

- Gecikmeli stop regresyonu **session 50039 / exit 0**: üç durum sorgusu
  boyunca `stop_requested → stopped` görüldü; beklerken tekrar stop devre
  dışıydı. Aynı/değişen payload key'leri, süit sınırı ve rapor yeniden geçti.
  `review-evidence/local-console-stop-review.json` son App/build hash'lerini
  içerir. Bu testte tüm deney yanıtları fixture'dır; gerçek koşu veya CPU
  kontrolü başlatılmadı.
- Son görsel kontrol **session 84174 / exit 0**: **1505×1045 masaüstü**,
  **390×844 mobil**, dört menü görünür, yatay taşma yok. Tek tarayıcı origin'i
  `127.0.0.1:8788`. `review-evidence/local-console-final-visual.json` son
  App/build hash'lerine bağlıdır. Ekran görüntüleri `view_image` ile incelendi.
- Kendi konsol servisinin önceki PID/InvocationID/ExecStart kimliği ve aktif
  kontrol yokluğu doğrulandıktan sonra yalnız bu servis yeniden başlatıldı.
  Başlatıcı gerçek **exit 0**; yeni generation ve kaynak kimlikleri
  `review-evidence/local-console-deployment.json` içindedir. Lab API ve model
  etkinliği kapalı, kabul sayıları 10/8/4 olarak doğrulandı.

## Tasarım karşılaştırması

Önce Image Gen ile [konsept](ui/console-concept.png) üretildi. Browser/IAB
aracı bulunmadığından Playwright Chromium kullanıldı. Son
[masaüstü](ui/console-desktop-full.png) ve [mobil](ui/console-mobile.png)
ekranları konseptle birlikte `view_image` üzerinden görsel olarak incelendi.
Arayüz görselin içine gömülü değildir; gerçek HTML kontrolleri kullanır.

| İnceleme noktası | Son uygulama |
|---|---|
| Yerleşim | Konseptteki sol gezinme, geniş ana alan, durum kartı, işler/sistem sütunları ve deney bölümü korunur; sidebar 220 px ölçüldü. |
| Başlık ve metin | “Araştırma kontrol merkezi”, alt metin, dört menü ve “Kontrolü çalıştır” aynıdır; gereksiz üst etiket/başlık ikonları kaldırıldı. |
| Tipografi | Başlık 32 px, ağırlık 720; sistem sans, açık hiyerarşi ve sayısal hizalama. Raster referansın fontunu bire bir kopyalama hedeflenmedi. |
| Renk | Beyaz ana alan, açık gri sol alan, koyu yazı ve teal vurgu; durum renkleri metinlerle birlikte kullanılır. |
| Çizgi ve boşluk | İnce kenarlıklar, 8 px köşeler, tutarlı tablo/buton boşlukları; büyük gölge veya ek dekorasyon yok. |
| İkonlar | Tutarlı ince çizgili SVG ikonları; raster şekillerin yerine erişilebilir gerçek kontroller. |
| Mobil | 390 px'de iki sütunlu üst gezinme, alt alta içerik; dört menü görünür ve yatay taşma yok. Bu, masaüstü konseptin bilinçli uzantısıdır. |
| İşlevsel ekranlar | Kanıt penceresi, durum filtreleri, test sonucu, deney formu ve hata durumları aynı görsel kurallarla tamamlandı. |

**Bilinçli metin farkları:** konseptteki örnek RAM/GPU/API değerleri gerçek
ölçümle değiştirilir. Bağlantısız deney alanı “Lab API henüz yapılandırılmadı.”
der; GPU bildirimi “GPU denemeleri AOS koordinasyonunu bekliyor.” şeklindedir.
Gerçek kaynak yolu ve ölçüm zamanı eklendi. Bu alt bilgi 1045 px yüksekliğinde
hafif dikey kaydırma gerektirir; tam sayfa ekran görüntüsü tamamını içerir.
CPU başlangıç bildirimi kullanıcıyı Sistem ekranına yönlendiren sade metindir.
İnceleme sonunda bilinen bir yerleşim/taşma kusuru bırakılmadı.

## Kabul ve devam eden işler

Bu teslim kullanıcıya ilerlemeyi ve gerçek CPU kontrolünü görünür kılar;
M0 kapı durumlarını değiştirmez. Geçerli S2 araştırması, public araştırma
koşuları, gerçek holdout kabulü, QLoRA ölçümü ve AOS ile gerçek birlikte çalışma
açık kalır. Remote bulunmadığı için PR açılmadı; otomatik merge yapılmadı.

Commit öncesi tam staged whitespace kontrolü **exit 2** verdi: yalnız üç
ham `*.stdout.txt` kaydındaki upstream deprecation uyarılarının sondaki
boşlukları. Ham test çıktıları değiştirilmedi. Bu dosyalar hariç kaynak ve
belge kontrolü `git diff --cached --check -- . ':!*.stdout.txt'` **exit 0**.
