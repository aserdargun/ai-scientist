# Yerel AI Scientist kontrol arayüzü

Tarih: 2026-09-26. Kullanıcı, mevcut yapıyı test etmek ve tamamlanmayı görmek için
bir arayüz istedi. Bu ek yüzey mevcut M0 hedefinin parçasıdır; kabul kapılarını
kendiliğinden kapatmaz. SWAPP ve canlı AOS dosyaları değiştirilmez.

## Tasarım

Referans: `ui/console-concept.png` (Image Gen, tasarım referansı; veri kaynağı değildir).
1440 px masaüstünde 220 px sol gezinme, beyaz ana alan, 40 px dış boşluk,
32 px başlık, 16 px gövde, 13 px yardımcı metin. Renkler: beyaz #ffffff,
sol alan #f5f7fa, metin #192431, ikincil metin #64748b, çizgi #dce3eb,
vurgu #0c766e, kısmi #f5a524. İnce çizgiler, 8 px köşeler; büyük gölge yok.
Sistem sans yazı tipi; sayısal veriler tabular. İnce çizgili tutarlı ikonlar.

Gezinme: Genel bakış, Kabul maddeleri, Deneyler, Sistem. Mobilde üst gezinmeye
dönüşür. Genel bakışta kabul sayıları, 22 parçalı durum çubuğu, dört öncelikli
açık/kısmi kapı, canlı sistem, deney listesi ve UUID ile izleme bulunur.
Kabul sayıları markdown tablosundan türetilir; sabit sayılar veya tahmini iş
yüzdesi kullanılmaz. Her madde durum ve kanıt açıklamasıyla açılabilir.
Detay/form ekranları aynı tablo, buton, form ve bildirim bileşenlerini kullanır.

Üst metin: “Araştırma kontrol merkezi”, “Tamamlanmayı izle, deneyleri çalıştır,
kanıtları incele.” Ana eylem “Kontrolü çalıştır”. Kabul açıklaması: “Bu oran kabul
maddelerini gösterir; işin tamamlanma yüzdesi değildir.”

## Uygulama sınırı

- `console/web`: React + Vite + TypeScript; çalışma zamanında CDN/font/cloud yok.
- `console/server.py` giriş noktası ve `console/` Python modülleri: bağımsız
  FastAPI sunucusu; tek origin üzerinden derlenmiş UI.
- Loopback varsayılan adresi `http://127.0.0.1:8788`; ayrı process ve kaynak sınırı.
- Araştırma runtime dosyaları, harness hash'i ve Docker imajı bu UI için değiştirilmez.
- Yerel API token'ı yalnız sunucuda private dosyadan okunur. Tarayıcıya gönderilmez.
- Host/Origin denetimi ve JSON + özel istek başlığı, başka sitelerden komut başlatmayı engeller.
- GPU/model/broker/AOS/DB kendiliğinden başlatılmaz. API yapılandırılmamışken
  kabul ekranı, host ölçümleri ve CPU kontrolü kullanılabilir.
- Model deneyleri ayrıca mevcut AOS koordinasyonuna ve açık sunucu ayarına bağlıdır.
- Mevcut API'nin sahiplik, idempotency, suite registry ve bütçe kuralları korunur.
- Doküman kanıtları yalnız kabul kaydında referans verilen, proje doküman kökü
  içindeki izinli dosyalardan okunur; keyfi dosya/secret/komut erişimi yoktur.

## UI–sunucu sözleşmesi

Tüm UI istekleri `/console-api` altında; hatalar `{ "detail": "..." }`.
Değiştiren isteklerde `Content-Type: application/json` ve
`X-Lab-Console: 1` zorunlu. Kimlik bilgisi fetch'e dahil edilmez.

### GET /console-api/overview

```json
{
  "version": "0.29.0",
  "generated_at": "ISO8601 UTC",
  "acceptance": {
    "total": 22, "passed": 10, "partial": 8, "open": 4,
    "source": "docs/ai-scientist/m0-acceptance.md",
    "source_sha256": "sha256",
    "items": [{"id":"M0.10","title":"...","status":"open","detail":"...","evidence":[]}]
  },
  "system": {
    "memory": {"total_bytes":0,"available_bytes":0},
    "disk": {"total_bytes":0,"free_bytes":0},
    "cpu": {"logical_count":0,"load_1m":0.0},
    "gpu": {"available":false,"name":null,"total_mib":null,"used_mib":null,"utilization_percent":null,"temperature_c":null,"reason":"..."}
  },
  "lab": {
    "configured":false,"connected":false,"reason":"...",
    "model_runs_enabled":false,
    "suites":[{"suite_id":"...","track":"anomaly","program_version":"director.v1","provider":"fake-json","proposal_limit":1}]
  }
}
```

`evidence` elemanları `{name, url, kind}`; kind `document`, `json`, `html`, `text`.
Her alan gerçek gözleme dayanır. Ölçüm yoksa null/reason gösterilir.
GET `/console-api/evidence/{id}` yalnız sunucunun ürettiği opaque id kabul eder.
Yanıt `{name,kind,content,sha256,truncated}` JSON zarfıdır. En fazla 256 KiB metin
gösterilir; kesilme açıkça belirtilir, SHA tüm izinli kaynak dosyaya aittir.
HTML kanıtları güvenilir origin içinde aktif script çalıştıramaz; izin içermeyen
iframe sandbox/srcDoc veya escaped metin olarak çizilir. JSON/metinler escaped
text olarak çizilir. Dosyalar en fazla 4 MiB; daha büyük kanıt açık nedenle reddedilir.

### CPU kontrolü

POST `/console-api/checks` gövde `{}` → 202 `{id,state}`.
GET `/console-api/checks` → `{items:[...]}`.
GET `/console-api/checks/{id}` →
`{id,state,started_at,finished_at,exit_code,summary,output}`.
Durum `queued|running|passed|failed`. Tek aktif kontrol; ikinci istek 409.
Sabit, hızlı, yalnız CPU test allowlist'i; gerçek exit code ve sınırlı log.
Bu kontrol kabul sayısını değiştirmez ve gerçek model kabulü olarak gösterilmez.

### Deneyler

GET `/console-api/runs` → `{items:[...]}` (konsolda izlemeye eklenen koşular).
POST `/console-api/runs/watch` `{run_id: UUID}` → gerçek API doğrulaması sonrası durum.
GET `/console-api/runs/{uuid}` → mevcut `RunStatusResponse`.
POST `/console-api/runs` → mevcut `StartRunRequest`; sunucu kayıtlı suite ile doğrular;
`external_*` alanları kabul edilmez (local principal). Dönen mevcut `StartRunResponse`.
POST `/console-api/runs/{uuid}/stop` `{}` → mevcut `RunStatusResponse`.
GET `/console-api/runs/{uuid}/report` → mevcut hash doğrulamalı report JSON.
API yokken bu işlemler açık nedenli 503 verir; sahte koşu oluşturulmaz.
Liste satırında API erişilemiyorsa son durum açıkça eski/ulaşılamıyor etiketlenir.

Yeni deney formu yalnız sunucudan gelen suite'leri kullanır; adet 1..35 ve suite
tavanı, süre 1..14400, token 0..350000. `crypto.randomUUID()` ile oluşturulan
idempotency anahtarı aynı istek retry'ında korunur. Gönderim sırasında buton devre
dışı; hata inline görünür. Rapor ve stop yalnız uygun durumlarda etkin olur.

## Doğrulama

API/başlık/yol sınırları, gerçek markdown sayıları, subprocess çıkışının doğru
gösterimi ve proxy kimlik/bütçe davranışı hedefli testlerle kontrol edilir.
Tarayıcıda ana ekran, durum filtresi ve detay, gerçek CPU kontrolü, erişilemeyen
API davranışı, rapor/deney formu ve 390 px mobil taşma kontrol edilir.
Browser/IAB aracı mevcut olmadığı için Playwright Chromium kullanılır.
Referans ve son tarayıcı ekranı `view_image` ile karşılaştırılır. Gerçek Lab API
kanıtı yoksa proxy fixture testi açıkça ayrı kaydedilir.
