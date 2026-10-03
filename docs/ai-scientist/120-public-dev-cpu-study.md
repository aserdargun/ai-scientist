# Gerçek public DEV verisiyle CPU mod deneyi — 0.45.0

2026-10-03. Ayrı `feat/omr-stream-v1` yerel önizlemesi; native çekirdek 0.41.1.

## Arayüzde görün

**aserdargun:** http://HOST:8788/ · **CachyOS:** http://127.0.0.1:8789/.
**Deneyler → `92ae603c-e367-422b-92fa-4fd567f80548` → Rapor**.
OPTICS adayında OMR ve sekiz sensörün residual/tahmin tablolarını açın.
Gerçek terminal raporu masaüstü ve mobil tarayıcıda doğrulandı; 470 OMR
noktası göründü. aserdargun bilgisayarındaki tarayıcı ayrıca denenmedi.

Yeni sınırlı karşılaştırma için **Çalışma modları → Kurulu veri kaynağı**
alanına şu snapshot SHA-256 değerini yapıştırın:

```
964641ae824487aca2a83890bbec794a1eb8a19afed114b13b20b803660e0101
```

Bu kurulu kaynak en fazla iki aday, 600 saniye ve sıfır model tokenı kabul
eder. İlk çalışmada LSH ve OPTICS varsayılanları, seed 0 kullanıldı.
Kullanıcının yeni başlatacağı deney ayrı kaynak bütçesi ve kimlikle yürür.

## Ölçülen sonuç

| Alan | Gerçek sonuç |
|---|---|
| Veri | SKAB `valve1-0-fixed-source-session`, sabit DEV kesiti |
| Satırlar | 26 fit / 470 değerlendirme; 20 maskeli, 450 puanlanan örnek |
| Süre | 283,41 saniye; 600 saniyelik bütçe |
| Bağımsız ölçümler | 9 baseline + 3 OPTICS (ilk ölçüm ve iki teyit) |
| LSH | Guard tarafından reddedildi; skor üretilmedi |
| OPTICS | KEEP; geliştirme bileşik skoru 0,0487173 |
| Ham VUS-PR | OPTICS 0,590512; en iyi baseline ECOD 0,595436 |
| Model / GPU / eğitim | Sıfır çağrı / sıfır iş / başlatılmadı |
| Kapanış | Director ve 12 Scorer işi, cgroup ve sandbox temiz; kuyruk boş |

**KEEP, en iyi baseline'ın geçildiği anlamına gelmez.** Bu çalışmada ham
VUS-PR daha düşüktür. Genel iyileşme, saha doğruluğu veya öğrenilmiş model
iddiası yoktur. Rapor SHA-256:
`439bfef2e6f880838fe3d99b88299a30ea03fd0b285856d7c92446984e418a6e`.

## Veri ve kaynak sınırları

Önceden pinlenmiş public görev aynen korundu: kaynak satır aralıkları
fit `[89,115)`, değerlendirme `[730,1200)`. Yeni kesme/embargo uygulanmadı.
Fit etiket körü kaynak sırasıdır; normal veri garantisi yoktur. Kaynak UTC
bilgisi taşımadığından grafik ekseni kaynak sırasıdır; UTC veya örnekleme
düzenliliği uydurulmaz. Lisans ve atıf snapshot metadata'sında korunur.
Bu ek SKAB örneği, Genesis/GECCO/CATSv2 + SMD kabul kapsamını değiştirmez.

Mevcut profilin muhafazakâr CPU tavanı 5,5 çekirdektir. Director 1 CPU/2 GiB,
Scorer toplamı 1 CPU/2 GiB, Docker aşaması 2 CPU/4 GiB sınırlarını korudu.
Director gözlenen cgroup tepe belleği 226.717.696 bayt; son gözlem aralığında
host kullanılabilir belleğinin minimumu 17.632.763.904 bayttı. İkinci değer
tüm koşunun mutlak tepe RAM ölçümü değildir. GPU/VRAM/devir süreleri ölçülmedi.

## Geçen, kalan ve çalıştırılmayanlar

Geçen: kaynak/policy/owner bağlama; gerçek UI başlatma; bağımsız puanlama;
terminal rapor/OMR görünümü; mobil görünüm; eski raporların korunması;
Director/Scorer/sandbox temiz kapanışı. Tam kalite kapısı **3314 passed,
7 skipped, 177 deselected**; yedi komut exit 0; imajda 209 dosya eşleşti.

Kalan: AOS managed-native admission ve gerçek dışlama kanıtı, sonlu ortak
yetki bileşimi, ardından tek koordineli gerçek GPU devir ve iptal kabulü.
AOS'un çalışan oturumu değiştirilmedi. AOS uyumluluk aktarımı
[koordinasyon kaydındadır](06-aos-coordination.md).

Bu teslimde çalıştırılmayanlar: yerel model Public27 araştırması, holdout,
öğretmen eğitimi, LoRA/QLoRA ve öğrenilmiş skill/model terfisi. Deney hafızası
mevcuttur; her çalışmanın otomatik kalite artışı sağladığı kanıtlanmamıştır.
Proje lisansı, genel CI ve temiz kurulum işleri ayrı tutulur.

[Seçilmiş kanıt kaydı](review-evidence/field-lab-public-dev-study-20261003.json)
commitleri, zaman damgalı yerel fark hash'lerini, sözleşmeleri, ayarları,
kaynak/imaj ve UI/cleanup kanıtlarının hash'lerini içerir. Commit/push/merge
yapılmadı; değişiklikler yerel önizleme çalışma ağacındadır.
