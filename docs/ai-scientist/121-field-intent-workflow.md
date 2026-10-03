# Saha amacıyla deney tasarımı — 0.46.0

2026-10-03. Ayrı yerel Eylemci önizlemesi.

## Kullanım

**[aserdargun arayüzü](http://HOST:8788/)** → **Eylemci → Saha
araştırma amacı**. Varlık etiketini, dijital ikiz/kestirimci bakım amacını
ve kısa hedefi yazın. **Bu amaçla deney tasarla** ile veri/yöntem ekranına
geçin; amacı düzenleyebilir veya kaldırabilirsiniz. Amaç seçilen snapshot'a
bağlanarak deney isteğinde saklanır. Salt metin girilmesi varlık yetkisi,
eğitim izni veya bakım aksiyonu oluşturmaz.

Hazır tamamlanmış örnek: **`50ea6463-4589-4213-a2fe-817287f3a323`**.
**Deneyler → Rapor** ile ölçümleri; **Eylemci → Doğrulanmış deney hafızası**
içinde aynı koşuyu seçerek varlık/hedef, veri ve bağlam hash'lerini açın.

## Gerçek uçtan uca sonuç

- `DEMO-SKAB-VALVE-1` kullanıcı beyanı; kestirimci bakım araştırma amacı.
- Aynı hedef metni → sabit SKAB DEV snapshot → bir OPTICS önerisinin gerçek
  Director bağlamı ve trajectory kaydı → doğrulanmış terminal görünüm.
- **279,61 saniye**, 600 saniyelik bütçe, 9 baseline + 3 OPTICS skoru.
- OPTICS KEEP; ham VUS-PR 0,590512 < en iyi baseline 0,595436.
  Ölçülmüş genel iyileşme iddiası yoktur.
- Aynı isteğin tekrarı aynı koşuya döndü; yeni deney açılmadı.
- Rapor, 470 OMR noktası ve 8 sensör masaüstü/mobilde görüntülendi.
- Director ve 12 Scorer işi/cgroup ile sandbox temiz; kuyruk boş.
  AOS'un özgün supervisor/backend süreçleri değişmedi.
- Model/GPU çağrısı ve eğitim sıfır. Bu gerçek veri CPU deneyi,
  yerel modelin hedefi kullanarak daha iyi öneri ürettiğini kanıtlamaz.

Rapor SHA: `3d50f90bfa9767d6e5f76f0f8df65f812ff410d6035e12efdb1dfb9b750bf0fb`.
Bağlam SHA: `b12d46c67b4dd9e5956d71923538ece23c2e79988b1e535cd517e1178c05c8a4`.
Sözleşme `field-study-context.v1`; mevcut `lab.report.v1` değişmedi.
Amaç bilgisi rapora bağlı doğrulanmış eşlikçi görünümden okunur.
İncelenmemiş hedef metni SFT ihracından çıkarılır.

## Doğrulama

Tam kalite kapısı **3328 passed / 7 skipped / 177 deselected**, yedi komut
exit 0; imajda 210 kaynak dosyası eşleşti. İlk kapı çağrısındaki eksik PATH
ve tarayıcı yardımcısındaki cache/etiket seçici hataları saklandı; tarayıcı
hatalarında iş gönderilmedi. Kaynak kodu bu gözlem hataları için değiştirilmedi.

| Tarayıcı kontrolü | Sonuç |
|---|---|
| Yerel URL, başlık ve anlamlı sayfa | Geçti |
| Framework hata katmanı / JavaScript hatası | Yok |
| Form → gerçek istek → aynı hedefin terminal görünümü | Geçti |
| Kaynak/bağlam hash'i ve rapor ilişkisi | Geçti |
| Masaüstü ve mobil, taşma | Geçti; taşma yok |
| Salt okunur sonuç kontrolü | POST yok |

Playwright kullanıldı; Browser eklentisi mevcut değil. Yerel arayüz 8789,
aserdargun Tailscale köprüsü 8788. Bu kontrol macOS tarayıcısı kabulü değildir.
Director gözlenen tepe RAM'i 237.858.816 bayt; gözlem aralığında host
kullanılabilir belleği en az 17.677.541.376 bayttı. Mevcut bileşen kaynak
sınırları korundu; GPU/VRAM/devir gecikmesi ölçülmedi.

[Seçilmiş kanıt](review-evidence/field-lab-field-intent-20261003.json).
Kalanlar: yetkili saha varlık eşlemesi, hedefe göre yerel model öneri
faydasının ölçülmesi, holdout, öğretmen/adapter eğitimi ve gerçek AOS GPU
birlikte çalışma. Otomatik öğrenilmiş iyileşme veya saha bakım kararı yoktur.
